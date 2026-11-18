"""Inventory changes retain unrelated observations, not physical-session evidence."""
import asyncio
import unittest
from avionmesh_pi.lifecycle import reload_inventory
import test_lifecycle as base
UUID = base.UUID
from avionmesh_pi.association import uuid_hash

class InventoryStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base.LifecycleTests.asyncSetUp(self)
        inv=self.store.inventory()
        inv['devices'] += [{'device_id':40003,'product_id':93,'name':'Other shared light','exposed':True},
                           {'device_id':40004,'product_id':134,'name':'Other local light','exposed':False}]
        inv['groups'].append({'group_id':293,'name':'Unchanged room','members':[40003,40004],'exposed':True,'protected':False})
        self.store.import_inventory(inv);reload_inventory(self.runtime)
        self.observed={'brightness':(128,10),'color_temp':(4000,10)}
        self.runtime.core.states[40003]=dict(self.observed)
        self.runtime.local_light_states[40004]=dict(self.observed)
        self.runtime.local_light_states[40002]=dict(self.observed)
        self.output=[];self.runtime.sink=self.output.append
    asyncTearDown=base.LifecycleTests.asyncTearDown
    def assert_retained(self):
        for target in (40003,40004):
            self.assertEqual(self.runtime._light_observations(target),self.observed)
            self.assertTrue(self.runtime.device_states()[str(target)]['brightness']['fresh'])
        self.assertFalse(any(m['payload']=='offline' and m['topic'] in
            ('avionmesh_pi/pi_lab/light/40003/availability','avionmesh_pi/pi_lab/group/293/availability') for m in self.output))
    async def test_removal_start_and_completion_preserve_other_lights_without_offline_flash(self):
        result=await self.lifecycle.remove(40002);self.assert_retained()
        self.assertNotIn('40002',self.runtime.device_states())
        self.scan.results=[{'uuid':UUID,'uuid_hash':uuid_hash(bytes.fromhex(UUID))}]
        self.assertTrue((await self.lifecycle.reconcile(result['transaction']))['removed'])
        self.assert_retained();self.assertNotIn(40002,self.runtime.local_light_states)
        self.assertEqual(len(self.transport.frames),1)
    async def test_unconfirmed_removal_preserves_others_but_quarantines_target(self):
        result=await self.lifecycle.remove(40002)
        self.assertFalse((await self.lifecycle.reconcile(result['transaction']))['removed'])
        self.assert_retained();self.assertNotIn('40002',self.runtime.device_states())
    async def test_transport_failure_still_clears_every_light(self):
        self.transport.fail=True
        with self.assertRaises(RuntimeError):await self.lifecycle.remove(40002)
        self.assertFalse(self.runtime.device_states())
    async def test_real_session_boundary_does_not_preserve_cached_readings(self):
        self.runtime.session();self.assertFalse(self.runtime.device_states())
    async def test_offline_reload_cannot_preserve_old_readings(self):
        self.transport.ready=False;reload_inventory(self.runtime)
        self.assertFalse(self.runtime.device_states())
    async def test_added_device_does_not_inherit_readings_or_clear_existing_lights(self):
        inv=self.store.inventory();inv['devices'].append({'device_id':40005,'product_id':134,'name':'New light','exposed':False})
        self.store.set('inventory',inv);reload_inventory(self.runtime)
        self.assert_retained();self.assertNotIn('40005',self.runtime.device_states())
    async def test_retyped_and_exposure_changed_devices_require_new_readings(self):
        inv=self.store.inventory()
        for d in inv['devices']:
            if d['device_id']==40003:d['product_id']=134
            if d['device_id']==40004:d['exposed']=True
        self.store.set('inventory',inv);reload_inventory(self.runtime)
        for target in (40003,40004):self.assertNotIn(str(target),self.runtime.device_states())
    async def test_rename_keeps_original_observation_times(self):
        inv=self.store.inventory()
        for d in inv['devices']:d['name']+=' renamed'
        self.store.set('inventory',inv);reload_inventory(self.runtime);self.assert_retained()
    async def test_preserved_stale_observations_are_not_made_fresh(self):
        self.runtime.clock=lambda:130001;reload_inventory(self.runtime)
        for target in (40003,40004):
            state=self.runtime.device_states()[str(target)]['brightness']
            self.assertFalse(state['fresh']);self.assertEqual(state['observed_ms'],10)
        self.assertTrue(any(m['topic']=='avionmesh_pi/pi_lab/light/40003/availability' and m['payload']=='offline' for m in self.output))
    async def test_inventory_boundary_still_cancels_waiters_and_controller_evidence(self):
        future=asyncio.get_running_loop().create_future();self.runtime.waiters.append((None,future,None))
        self.runtime.controller_events[40001]={'event':'synthetic'};generation=self.runtime.generation
        reload_inventory(self.runtime)
        self.assertGreater(self.runtime.generation,generation);self.assertFalse(self.runtime.controller_events)
        with self.assertRaises(RuntimeError):await future
        self.assert_retained()

    async def test_disconnected_display_history_never_becomes_control_or_mqtt_evidence(self):
        self.runtime.mqtt_ready=True;self.transport.ready=False;self.runtime.mesh_ready=False
        self.runtime.session()
        self.assertFalse(self.runtime.device_states())
        history=self.runtime.last_known_device_states()
        self.assertEqual(history['40003']['brightness'],{'value':128,'observed_ms':10,'age_ms':0,'fresh':False})
        self.assertFalse(self.runtime._light_observations(40003))
        self.transport.ready=True;self.runtime.mesh_ready=True;self.runtime.session()
        self.assertEqual(self.runtime.last_known_device_states(),history)
        self.output.clear();self.runtime.publish_current_states()
        self.assertFalse(any(m['topic'].endswith('/state') for m in self.output))
        self.assertTrue(any(m['topic'].endswith('/light/40003/availability') and m['payload']=='offline' for m in self.output))
    async def test_display_history_keeps_original_age_across_repeated_reconnects(self):
        self.runtime.session();self.runtime.clock=lambda:30000;self.runtime.session()
        reading=self.runtime.last_known_device_states()['40004']['brightness']
        self.assertEqual(reading['observed_ms'],10);self.assertEqual(reading['age_ms'],29990)
        self.assertFalse(reading['fresh'])
    async def test_new_power_report_overrides_old_power_but_not_missing_colour(self):
        self.runtime.local_light_states[40004]['state']=('OFF',10)
        self.runtime.session();self.runtime.clock=lambda:30;self.runtime.local_light_states[40004]={'brightness':(255,20)}
        fallback=self.runtime.last_known_device_states()['40004']
        self.assertNotIn('state',fallback);self.assertNotIn('brightness',fallback)
        self.assertEqual(fallback['color_temp']['value'],4000)
        self.runtime.session()
        self.assertEqual(self.runtime.last_known_device_states()['40004']['brightness']['value'],255)
        self.assertNotIn('state',self.runtime.last_known_device_states()['40004'])
    async def test_removed_and_retyped_devices_do_not_keep_display_history(self):
        self.runtime.session();inv=self.store.inventory()
        inv['devices']=[d for d in inv['devices'] if d['device_id']!=40004]
        for d in inv['devices']:
            if d['device_id']==40003:d['product_id']=134
        self.store.set('inventory',inv);reload_inventory(self.runtime)
        self.assertNotIn('40003',self.runtime.last_known_device_states())
        self.assertNotIn('40004',self.runtime.last_known_device_states())
        self.assertNotIn(40004,self.runtime._display_light_history)
    async def test_quarantined_removal_has_no_last_known_display_value(self):
        self.runtime.session()
        await self.lifecycle.remove(40002)
        self.assertNotIn('40002',self.runtime.last_known_device_states())
        self.assertIn('40003',self.runtime.last_known_device_states())
    async def test_display_history_cannot_confirm_command_feedback(self):
        self.runtime.session()
        self.runtime.local_light_states[40003]={'brightness':(255,20)};self.runtime.clock=lambda:30
        self.assertTrue(self.runtime.device_states()['40003']['brightness']['fresh'])
        self.assertNotIn('brightness',self.runtime.last_known_device_states()['40003'])
        self.assertFalse(self.runtime.device_states().get('40004'))

    def add_unread_light(self):
        inv=self.store.inventory();inv['devices'].append({'device_id':40005,'product_id':134,'name':'New bulb','exposed':False})
        self.store.set('inventory',inv);reload_inventory(self.runtime)
    async def test_new_light_gets_bounded_initial_status_without_inventing_readings(self):
        self.add_unread_light()
        self.assertEqual(self.runtime.initial_light_status()['40005'],{'remaining_ms':120000})
        self.assertNotIn('40005',self.runtime.device_states())
        self.runtime.clock=lambda:60010;reload_inventory(self.runtime)
        self.assertEqual(self.runtime.initial_light_status()['40005']['remaining_ms'],60000)
        self.runtime.clock=lambda:120010
        self.assertNotIn('40005',self.runtime.initial_light_status())
        self.assertFalse(self.transport.frames)
    async def test_first_power_report_ends_initial_status_even_after_reconnect(self):
        from avionmesh_pi.protocol import encrypt_mcp
        self.add_unread_light()
        self.runtime.observe(encrypt_mcp(bytes.fromhex('008073011d00000fa0'),base.KEY,100,40005),10)
        self.assertIn('40005',self.runtime.initial_light_status(),'Colour alone does not establish power')
        self.runtime.observe(encrypt_mcp(bytes.fromhex('008073010a0080'),base.KEY,101,40005),10)
        self.assertNotIn('40005',self.runtime.initial_light_status())
        self.assertEqual(self.runtime.device_states()['40005']['brightness']['value'],128)
        self.runtime.session()
        self.assertNotIn('40005',self.runtime.initial_light_status(),'Reconnect must not restart a completed first read')
    async def test_removed_new_light_and_controllers_have_no_initial_status(self):
        self.add_unread_light();inv=self.store.inventory()
        inv['devices']=[d for d in inv['devices'] if d['device_id']!=40005]
        inv['devices'].append({'device_id':40006,'product_id':127,'name':'New controller','exposed':False})
        self.store.set('inventory',inv);reload_inventory(self.runtime)
        self.assertNotIn('40005',self.runtime.initial_light_status())
        self.assertNotIn('40006',self.runtime.initial_light_status())
        self.assertNotIn(40005,self.runtime._initial_light_deadlines)
    async def test_old_unknown_light_does_not_gain_a_grace_period_on_metadata_update(self):
        self.runtime._initial_light_deadlines.clear();self.runtime.local_light_states.clear();self.runtime.core.states.clear()
        reload_inventory(self.runtime)
        self.assertEqual(self.runtime.initial_light_status(),{})
