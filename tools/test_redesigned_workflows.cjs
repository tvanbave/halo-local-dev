/* Offline browser QA. All requests are intercepted; no bridge is contacted. */
const fs = require('fs'), path = require('path'), assert = require('assert');
const pw = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const engine = process.env.BROWSER_ENGINE || 'chromium';
const root = path.resolve(__dirname, '..');
const evidence = process.env.EVIDENCE_DIR || path.join(root, 'docs/GROUP-CONTROLLER-UI-20261111-evidence');
const clone = x => JSON.parse(JSON.stringify(x));
function fixture() {
  return {status:{mesh_ready:true,mqtt_enabled:false,mqtt_ready:false,preview_only:false,pending_transactions:0,jobs:{},instance:'one',inventory_revision:'r1',bootstrap:{needs_first_claim:false}},
    inventory:{devices:[{device_id:40001,product_id:93,name:'Theatre 1'},{device_id:40002,product_id:93,name:'Theatre 2'},{device_id:40003,product_id:127,name:'Wall keypad'}],groups:[{group_id:300,name:'Theatre',members:[],protected:false}]},
    group_workflow:{programming_enabled:true,lights:[{target:40001,name:'Theatre 1',eligible:true},{target:40002,name:'Theatre 2',eligible:true}]},
    controllers:[{device_id:40003,ha_status:'disabled',assignment_read_back:false,slots:[],actions:{read_assignments:{enabled:true}},last_event:null}],device_states:{}};
}
(async () => {
  fs.mkdirSync(evidence,{recursive:true});
  const browser = await pw[engine].launch(engine === 'chromium' ? {headless:true,executablePath:process.env.CHROME_EXECUTABLE,args:['--disable-background-networking']} : {headless:true});
  const results = [], errors = [];
  let scenarioIndex = 0;
  const shard = process.env.SCENARIO_SHARD?.split('/').map(Number);
  if (shard) assert(shard.length === 2 && shard.every(Number.isInteger) && shard[0] >= 0 && shard[0] < shard[1], 'SCENARIO_SHARD must be zero-based index/count');
  async function scenario(name, test, mobile=false) {
    if(process.env.SCENARIO_PATTERN && !new RegExp(process.env.SCENARIO_PATTERN).test(name))return;
    if (shard && scenarioIndex++ % shard[1] !== shard[0]) return;
    const context = await browser.newContext({viewport:mobile?{width:390,height:844}:{width:1280,height:960},isMobile:mobile,hasTouch:mobile,serviceWorkers:'block'});
    const page = await context.newPage(); page.setDefaultTimeout(5000);
    const state = {snapshot:fixture(),calls:[],handlers:{},features:{},dialogs:[],snapshotFail:false};
    page.on('pageerror',e=>errors.push(name+': '+e.message));
    page.on('dialog',d=>{state.dialogs.push(d.message());return d.accept();});
    await context.route('**/*',async route=>{
      const req=route.request(), url=new URL(req.url());
      if(url.origin!=='http://synthetic.invalid')return route.abort();
      const assets={'/':['ui.html','text/html'],'/ui.js':['ui.js','text/javascript'],'/settings.js':['settings.js','text/javascript'],'/backup.js':['backup.js','text/javascript'],'/ops.js':['ops.js','text/javascript'],'/ui.css':['ui.css','text/css']};
      if(assets[url.pathname]){const [f,t]=assets[url.pathname];return route.fulfill({contentType:t,body:fs.readFileSync(path.join(root,'avionmesh_pi/web',f),'utf8')});}
      let body;try{body=req.postDataJSON();}catch{}
      state.calls.push({path:url.pathname,body});
      const send=(data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
      if(state.handlers[url.pathname])return state.handlers[url.pathname]({body,send,route});
      if(url.pathname==='/api/capabilities')return send({features:{control:true,pair_lights:true,labels:true,group_metadata:true,group_edit:true,pairable_types:[93,134,127,126,91,81],...state.features},types:{controllers:{127:'Wired keypad',126:'Battery keypad',91:'Accessory dimmer',81:'Anyplace dimmer'},lights:{93:'RL4'},color_temp:[93]}});
      if(url.pathname==='/api/snapshot')return send(clone(state.snapshot),state.snapshotFail?503:200);
      if(url.pathname==='/api/events')return route.fulfill({contentType:'text/event-stream',body:''});
      if(url.pathname==='/api/metadata'){
        state.snapshot.inventory.groups.push({group_id:301,name:body.name,members:[],protected:false});state.snapshot.status.inventory_revision='r2';
        return send({saved:true,revision:'r2',group_id:301,physical_memberships_changed:false});
      }
      if(url.pathname==='/api/rooms/membership'){
        const g=state.snapshot.inventory.groups.find(g=>g.group_id===body.group);g.members=body.member?[...new Set([...g.members,...body.targets])]:g.members.filter(t=>!body.targets.includes(t));
        return send({physical_memberships_verified:true,message:'Selected lights have verified physical membership.',rows:body.targets.map(target=>({target,status:'verified'}))});
      }
      if(url.pathname==='/api/keypad/read')return send({accepted:true,completed:false,job_id:'read-one'},202);
      return send({error:'unexpected_mock_request'},404);
    });
    try{await page.goto('http://synthetic.invalid');await page.getByRole('button',{name:'Groups',exact:true}).waitFor();await page.waitForFunction(()=>S.synced);await test(page,state);results.push({name,passed:true});}
    catch(e){results.push({name,passed:false,error:e.message});await page.screenshot({path:path.join(evidence,name+'-failure.png'),fullPage:true});}
    finally{await context.close();}
  }
  await scenario('bluetooth-startup-retry-and-disconnection',async(p,s)=>{
    const status=s.snapshot.status;
    const update=()=>p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    Object.assign(status,{mesh_ready:false,bluetooth_starting:true,last_error:'bluetooth_connection_failed',pending_transactions:1});
    await update();
    const connecting=p.getByText('Connecting to the lights over Bluetooth…',{exact:true});
    const warning=p.getByText('Bluetooth is disconnected',{exact:true});
    await connecting.waitFor();
    assert(await p.getByText('1 unfinished operation needs review',{exact:true}).isVisible());
    assert.equal(await warning.count(),0);
    assert.equal(await p.getByText('The bridge reported a problem',{exact:true}).count(),0);
    assert.equal(await p.getByText('bluetooth_connection_failed',{exact:true}).count(),0);
    await p.getByRole('button',{name:'Settings',exact:true}).first().click();
    const bluetoothStatus=p.locator('dt').filter({hasText:/^Bluetooth$/}).locator('+ dd');
    await bluetoothStatus.getByText('Connecting',{exact:true}).waitFor();
    await p.evaluate(()=>go('home'));
    status.bluetooth_startup_retrying=true;
    await update();
    await p.getByText("Bluetooth hasn't connected yet",{exact:true}).waitFor();
    assert.equal(await warning.count(),0);
    status.bluetooth_startup_retrying=false;
    status.bluetooth_starting=false;
    await update();
    await warning.waitFor();
    assert.equal(await connecting.count(),0);
    assert.equal(await warning.count(),1);
    Object.assign(status,{mesh_ready:true,last_error:null});
    await update();
    await warning.waitFor({state:'detached'});
    assert.equal(await p.evaluate(()=>S.ble),4);
    status.mesh_ready=false;
    await update();
    await warning.waitFor();
    status.last_error='another_problem';
    await update();
    await p.getByText('another_problem',{exact:true}).waitFor();
  });
  await scenario('capabilities-recover-without-reloading-and-follow-restart',async(p,s)=>{
    const caps=await p.evaluate(()=>S.caps);
    let available=false, requests=0;
    s.handlers['/api/capabilities']=({send})=>{
      requests++;
      return available ? send({...caps,instance:s.snapshot.status.instance}) : send({},503);
    };
    await p.reload();
    await p.getByText('Loading device types…').waitFor();
    assert.equal(await p.evaluate(()=>lights().length),0);
    available=true;
    await p.waitForFunction(()=>S.synced && controllers().length>0);
    assert(requests>=2);
    const before=requests;
    let releaseOld, startedOld;
    const pendingOld=new Promise(resolve=>{startedOld=resolve;});
    const old=clone(s.snapshot);
    s.handlers['/api/snapshot']=async({send})=>{
      startedOld();
      await new Promise(resolve=>{releaseOld=resolve;});
      return send(old);
    };
    const delayed=p.evaluate(()=>refreshSnapshot());
    await pendingOld;
    s.snapshot.status.instance='after-restart';
    s.snapshot.status.bootstrap={needs_first_claim:true,automatic:true};
    s.snapshot.status.pairing={models:[93,134],attempts_remaining:6};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.waitForFunction(()=>S.synced && S.caps.instance==='after-restart');
    releaseOld();
    await delayed;
    assert.equal(await p.evaluate(()=>S.status.instance),'after-restart');
    assert(requests>before);
    await p.evaluate(()=>go('add'));
    await p.getByText(/6 pairing attempts available/).waitFor();
    assert(await p.evaluate(()=>pairable().includes(134)));
    s.snapshot.status.pairing={models:[93],attempts_remaining:0};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.waitForFunction(()=>document.querySelector('button[onclick="startSearch()"]')?.disabled);
  });
  const group = async p => {await p.evaluate(()=>{go('groups');openDrawer('group',300);});await p.getByRole('button',{name:'Add lights',exact:true}).waitFor();};
  const reported = (brightness, kelvin=3000, fresh=true) => ({
    state:{value:brightness ? 'ON':'OFF',fresh,observed_ms:1,age_ms:fresh?0:120000},
    brightness:{value:brightness,fresh,observed_ms:1,age_ms:fresh?0:120000},
    color_temp:{value:kelvin,fresh,observed_ms:1,age_ms:fresh?0:120000}});
  const groupSnapshot = async(p,s) => {
    s.snapshot.inventory.groups[0].members=[40001,40002];
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await group(p);
  };
  const keypad = async(p,s) => {
    Object.assign(s.snapshot.status,{mqtt_enabled:true,mqtt_ready:true});
    s.features.keypad_edit=true;
    s.snapshot.inventory.groups.push(
      {group_id:301,name:'Bar',members:[40002],protected:false},
      {group_id:24575,name:'HA keypad target',members:[],protected:true});
    s.snapshot.status.keypad_saves={};
    s.snapshot.controllers[0]={device_id:40003,product_id:127,ha_status:'disabled',assignment_read_back:true,
      revision:'keypad-rev-1',last_event:null,
      actions:{read_assignments:{enabled:true},edit_assignment:{enabled:true}},
      slots:[{slot:1,target:300,preset:200,status:'verified_mapping',editable:true,ha_available:true},
        {slot:2,target:65535,preset:0,status:'unused',editable:true,ha_available:true},
        {slot:3,target:24575,preset:255,status:'verified_mapping',editable:false},
        {slot:4,target:65535,preset:0,status:'unused',editable:true,ha_available:true}]};
    await p.evaluate(async snapshot=>{S.caps=await api('/api/capabilities');applySnapshot(snapshot);openDrawer('device',40003);},clone(s.snapshot));
    await p.locator('#kpG1').waitFor();
  };
  const blockKeypad = (s,reason) => {
    Object.assign(s.snapshot.controllers[0],{assignment_read_back:false,revision:null,slots:[],
      actions:{read_assignments:{enabled:false,reason},edit_assignment:{enabled:false,reason}}});
  };
  const acceptKeypad = (s,buttons=[{slot:2,state:'writing'}]) => {
    s.snapshot.status.jobs[40003]={id:'save-one',state:'running'};
    s.snapshot.status.keypad_saves[40003]={state:'running',buttons};
    blockKeypad(s,'operation_in_progress');
  };
  const accessory = async(p,s,enabled=true) => {
    Object.assign(s.snapshot.status,{mqtt_enabled:true,mqtt_ready:true});
    Object.assign(s.snapshot.inventory.devices[2],{product_id:91,name:'Test accessory dimmer'});
    s.snapshot.controllers[0]={device_id:40003,product_id:91,ha_status:'not_exposed',
      assignment_read_back:false,slots:[],last_event:null,
      accessory_ha:{state:'not_configured',target:null,observed_in_session:false},
      actions:{setup_ha:{enabled,reason:enabled?'available':'model_setup_disabled'},
        assign_native_target:{enabled:false,reason:'model_editor_disabled'}}};
    await p.evaluate(snapshot=>{applySnapshot(snapshot);openDrawer('device',40003);},clone(s.snapshot));
    await p.getByRole('button',{name:'Set up Home Assistant control',exact:true}).waitFor();
  };
  const beginAccessory = async p => {
    await p.getByRole('button',{name:'Set up Home Assistant control',exact:true}).click();
    await p.getByRole('button',{name:'Start HA setup',exact:true}).click();
  };
  const pendingAccessory = s => {
    s.snapshot.status.jobs[40003]={id:'acc-one',state:'running'};
    s.snapshot.status.pending_transactions=1;
    s.snapshot.controllers[0].actions.setup_ha={enabled:false,reason:'resolve_pending_operations'};
    s.snapshot.controllers[0].accessory_ha.state='needs_review';
  };
  const anyplace = async(p,s,enabled=true) => {
    Object.assign(s.snapshot.status,{mqtt_enabled:true,mqtt_ready:true,anyplace_progress:{}});
    Object.assign(s.snapshot.inventory.devices[2],{product_id:81,name:'Spare Anyplace dimmer'});
    s.snapshot.controllers[0]={device_id:40003,product_id:81,ha_status:'not_exposed',assignment_read_back:false,
      slots:[],last_event:null,anyplace_ha:{state:'not_configured',target:null,transaction:null,
        observed_in_session:false,experimental:true,assignment_read_back:false,original_assignment:'unknown'},
      actions:{setup_ha:{enabled,reason:enabled?'available':'model_setup_disabled'},
        assign_native_target:{enabled:false,reason:'model_editor_disabled'}}};
    await p.evaluate(snapshot=>{applySnapshot(snapshot);openDrawer('device',40003);},clone(s.snapshot));
    await p.getByRole('button',{name:'Set up experimental HA control',exact:true}).waitFor();
  };
  const beginAnyplace = async p => {
    await p.getByRole('button',{name:'Set up experimental HA control',exact:true}).click();
    await p.getByRole('button',{name:'Start experimental setup',exact:true}).click();
  };
  const finishAnyplace = s => {
    s.snapshot.status.jobs[40003]={id:'anyplace-setup-one',state:'completed',result:{transaction:'anyplace-tx',ready:true,
      native_behavior_observed:true,assignment_read_back:false,original_assignment:'unknown'}};
    s.snapshot.status.anyplace_progress[40003]='complete';
    s.snapshot.status.pending_transactions=0;
    s.snapshot.controllers[0].anyplace_ha={state:'configured',transaction:'anyplace-tx',target:24573,
      experimental:true,observed_in_session:false,assignment_read_back:false,original_assignment:'unknown',
      ha_receipt_verified:false,physical_isolation_verified:false};
    s.snapshot.controllers[0].actions.setup_ha={enabled:false,reason:'fresh_retained_claim_required'};
  };
  await scenario('keyboard-opens-and-returns-to-inventory-name',async(p)=>{
    for (const [view,name] of [['Devices','Theatre 1'],['Controllers','Wall keypad'],['Groups','Theatre']]) {
      const nav = p.locator('#navItems').getByRole('button',{name:view,exact:true});
      await nav.focus(); await p.keyboard.press('Enter');
      const target = p.locator('tbody').getByRole('button',{name,exact:true});
      const openerId = await target.getAttribute('id');
      assert(openerId, 'The inventory button must have a stable ID');
      let reached = false;
      for (let step=0;step<35;step++) {
        await p.keyboard.press(engine === 'webkit' && process.platform === 'darwin' ? 'Alt+Tab' : 'Tab');
        if (await p.evaluate(id=>document.activeElement.id===id,openerId)) { reached=true; break; }
      }
      assert(reached,'Keyboard should reach '+name);
      await p.keyboard.press('Enter');await p.locator('.drawer').waitFor();
      await p.keyboard.press('Escape');
      await p.locator('.drawer').waitFor({state:'detached'});
      // Inventory redraws replace row nodes. Check the current focused button in
      // one browser operation, then verify the next redraw preserves that focus.
      for (const redraw of [false,true]) {
        const activeId = await p.evaluate(update=>{
          if (update) render();
          return document.activeElement.id;
        },redraw);
        assert.equal(activeId,openerId,'Focus should return to '+name+' and survive a redraw');
      }
    }
  });
  await scenario('receipt-link-opens-only-its-operation',async(p,s)=>{
    const record=(id,target)=>({id,target,kind:'remove',status:'uncertain',detail:{},recovery:{actions:[]}});
    s.handlers['/api/transactions']=({send})=>send({transactions:[record('other',40001),record('wanted',40002)]});
    await p.evaluate(()=>{go('devices');openDrawer('device',40002);Object.assign(opsTool(40002),{review:true,receipt:'wanted'});render();});
    await p.getByRole('button',{name:'Review this operation'}).click();
    await p.getByRole('heading',{name:'Removal · Theatre 2'}).waitFor();
    assert.equal(await p.locator('.ops-card').count(),1);
    await p.getByRole('button',{name:'All operations'}).click();
    assert.equal(await p.locator('.ops-card').count(),2);
  });
  for (const mobile of [false,true]) await scenario('first-power-read-'+(mobile?'mobile':'desktop'),async(p,s)=>{
    s.features.read_states=true;
    s.snapshot.device_states={};
    s.snapshot.power_read_status={'40001':'waiting','40002':'waiting'};
    const push=()=>p.evaluate(x=>applySnapshot(x),clone(s.snapshot));
    await p.reload();
    const warning=p.getByText(/reported a state$/);
    await p.getByText('Getting status for 2 lights…').waitFor();
    assert.equal(await warning.count(),0);
    s.snapshot.power_read_status['40001']='retry_pending';
    await push();
    await p.getByText('Waiting for next check…',{exact:true}).waitFor();
    assert.equal(await warning.count(),0);
    await p.reload();
    await p.getByText('Waiting for next check…',{exact:true}).waitFor();
    assert.equal(await warning.count(),0);
    s.snapshot.power_read_status['40001']='no_reply';
    await push();
    await p.getByText("1 light hasn't reported a state").waitFor();
    assert.equal(await p.getByText('Getting status…',{exact:true}).count(),1);
    await p.reload();
    await p.getByText("1 light hasn't reported a state").waitFor();
    await p.locator('.attention').getByRole('button',{name:'View lights',exact:true}).click();
    await p.getByText('No power reading',{exact:true}).waitFor();
    assert.equal(await p.getByRole('button',{name:'Theatre 2',exact:true}).count(),0);
    await p.evaluate(()=>go('home'));
    s.snapshot.device_states={'40001':reported(0)};
    s.snapshot.power_read_status={};
    await push();
    await warning.waitFor({state:'detached'});
    assert.equal(await p.evaluate(()=>known(dev(40002))),false);
    assert(!s.calls.some(c=>c.body));
  },mobile);
  for (const mobile of [false,true]) await scenario('new-light-getting-status-'+(mobile?'mobile':'desktop'),async(p,s)=>{
    s.features.read_states=true;s.snapshot.inventory.groups[0].members=[40001,40002];
    s.snapshot.device_rooms={'40001':'Theatre','40002':'Theatre'};
    s.snapshot.device_states={'40002':reported(0)};
    s.snapshot.initial_light_status={'40001':{remaining_ms:120000}};
    const push=async()=>p.evaluate(snapshot=>{applySnapshot(snapshot);go('home');},clone(s.snapshot));
    await p.evaluate(async()=>{S.caps=await api('/api/capabilities')});await push();
    await p.getByText('Getting status…',{exact:true}).waitFor();
    assert.equal(await p.getByText(/They may be switched off/).count(),0);
    assert.equal(await p.evaluate(()=>known(dev(40001))),false,'Getting status must not invent a state');
    await p.reload();await p.waitForFunction(()=>S.synced);await p.evaluate(()=>go('home'));
    await p.getByText('Getting status…',{exact:true}).waitFor();
    // A different, older missing light still deserves the normal warning.
    delete s.snapshot.device_states['40002'];await push();
    await p.getByText("1 light hasn't reported a state",{exact:true}).waitFor();
    assert.equal(await p.getByText('Getting status…',{exact:true}).count(),1);
    // A real report immediately replaces the waiting display.
    s.snapshot.device_states['40001']=reported(128);await push();
    await p.getByText('50%',{exact:true}).waitFor();
    assert.equal(await p.getByText('Getting status…',{exact:true}).count(),0);
    // No report within the grace window: do not conceal a reachability issue forever.
    delete s.snapshot.device_states['40001'];s.snapshot.initial_light_status['40001'].remaining_ms=0;await push();
    await p.getByText("2 lights haven't reported a state",{exact:true}).waitFor();
    assert.equal(await p.getByText('Getting status…',{exact:true}).count(),0);
    assert(!s.calls.some(c=>c.body),'Status display must not send device commands');
  },mobile);
  for (const mobile of [false,true]) await scenario('last-known-reconnect-'+(mobile?'mobile':'desktop'),async(p,s)=>{
    s.snapshot.inventory.groups[0].members=[40001,40002];
    s.snapshot.device_rooms={'40001':'Theatre','40002':'Theatre'};
    s.snapshot.device_states={'40001':reported(128),'40002':reported(0)};
    const push=async()=>p.evaluate(snapshot=>{applySnapshot(snapshot);go('home');},clone(s.snapshot));
    await push();
    s.snapshot.status.mesh_ready=false;
    s.snapshot.last_known_device_states=clone(s.snapshot.device_states);
    s.snapshot.device_states={};await push();
    await p.getByText('Last known: 50%',{exact:true}).waitFor();
    assert.equal(await p.getByText('State unknown',{exact:true}).count(),0);
    assert(await p.getByRole('checkbox',{name:'Theatre power',exact:true}).isChecked());
    assert.equal(await p.evaluate(()=>dev(40001).brightness_fresh),false,'Fallback cannot confirm command intent');
    // A page reload gets display history from the bridge, not browser storage.
    await p.reload();await p.waitForFunction(()=>S.synced);await p.evaluate(()=>go('home'));
    await p.getByText('Last known: 50%',{exact:true}).waitFor();
    s.snapshot.status.mesh_ready=true;
    s.snapshot.device_states={'40001':{brightness:{value:255,observed_ms:2,age_ms:0,fresh:true}}};
    await push();await p.getByText('100%',{exact:true}).waitFor();
    assert.equal(await p.getByText('Last known: 50%',{exact:true}).count(),0);
    assert.equal(await p.evaluate(()=>dev(40001).color_temp_fresh),false);
    assert.equal(await p.evaluate(()=>dev(40001).brightness_fresh),true);
    // Missing new-device readings must remain unknown, never borrowed from another light.
    s.snapshot.inventory.devices.push({device_id:40004,product_id:93,name:'New light'});
    await push();await p.getByText('State unknown',{exact:true}).waitFor();
    assert(!s.calls.some(c=>c.body),'Rendering reconnect state sends no commands');
  },mobile);
  await scenario('anyplace-experimental-setup-gates-and-review',async(p,s)=>{
    await anyplace(p,s,false);
    assert(await p.getByRole('button',{name:'Set up experimental HA control',exact:true}).isDisabled());
    s.snapshot.controllers[0].actions.setup_ha={enabled:true,reason:'available'};s.snapshot.status.mqtt_ready=false;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(await p.getByRole('button',{name:'Set up experimental HA control',exact:true}).isDisabled());
    s.snapshot.status.mqtt_ready=true;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    await p.getByRole('button',{name:'Set up experimental HA control',exact:true}).click();
    assert(await p.locator('#modalRoot').getByText(/factory-reset spare freshly paired/).isVisible());
    assert(await p.getByText(/original assignment is unknown: it cannot be read, backed up, cleared or automatically restored/).isVisible());
    assert(await p.getByText(/No write is retried automatically/).isVisible());
    assert(!s.calls.some(c=>c.path==='/api/controller/setup'));
    await p.screenshot({path:path.join(evidence,'anyplace-experimental-review-mobile.png'),fullPage:true});
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  },true);
  await scenario('anyplace-exact-job-wake-phases-and-owned-completion',async(p,s)=>{
    await anyplace(p,s);
    s.handlers['/api/controller/setup']=({body,send})=>{assert.deepEqual(body,{target:40003});return send({accepted:true,completed:false,job_id:'anyplace-setup-one'},202);};
    await beginAnyplace(p);await p.getByText(/Experimental setup accepted/).waitFor();
    await p.evaluate(()=>confirmAnyplaceSetup());
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
    s.snapshot.status.jobs[40003]={id:'foreign-job',state:'running'};
    s.snapshot.status.anyplace_progress[40003]='waiting_for_wake';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert.equal(await p.getByText('Press the small Wake-Up button on the bottom once now. Wait for the next prompt.',{exact:true}).count(),0);
    s.snapshot.status.jobs[40003].id='anyplace-setup-one';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText('Press the small Wake-Up button on the bottom once now. Wait for the next prompt.',{exact:true}).waitFor();
    await p.evaluate(()=>window.anyplaceButton=document.querySelector('button[onclick="openAnyplaceSetup(40003)"]'));
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(await p.evaluate(()=>window.anyplaceButton===document.querySelector('button[onclick="openAnyplaceSetup(40003)"]')));
    s.snapshot.status.anyplace_progress[40003]='observing';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/Wake received. Use On, Off or the slider now/).waitFor();
    finishAnyplace(s);
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/Experimental setup recorded:/).waitFor();
    assert(await p.getByText('Experimental HA target configured',{exact:true}).isVisible());
    assert(await p.getByText(/Home Assistant receipt and physical isolation still need testing/).isVisible());
    assert(!await p.evaluate(()=>accessoryRequests.get(40003).review));
    assert.equal(await p.getByRole('button',{name:'Set up experimental HA control',exact:true}).count(),0);
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
    await p.screenshot({path:path.join(evidence,'anyplace-experimental-completion.png'),fullPage:true});
  });
  for(const failure of ['wrong-receipt','missing-receipt','unowned','no-observation','claims-readback','wrong-original','publication-pending','restart','lost-response']){
    await scenario('anyplace-setup-stops-'+failure,async(p,s)=>{
      await anyplace(p,s);
      s.handlers['/api/controller/setup']=({send})=>failure==='lost-response'
        ? send({error:'timeout_outcome_unknown'},504):send({accepted:true,completed:false,job_id:'anyplace-setup-one'},202);
      await beginAnyplace(p);
      if(failure==='lost-response')await p.getByText(/Setup outcome is not confirmed/).waitFor();
      else{
        await p.getByText(/Experimental setup accepted/).waitFor();
        if(failure==='restart')s.snapshot.status.instance='two';
        else{
          finishAnyplace(s);
          if(failure==='wrong-receipt')s.snapshot.controllers[0].anyplace_ha.transaction='foreign-transaction';
          if(failure==='missing-receipt'){delete s.snapshot.controllers[0].anyplace_ha.transaction;delete s.snapshot.status.jobs[40003].result.transaction;}
          if(failure==='unowned')s.snapshot.controllers[0].anyplace_ha.state='needs_review';
          if(failure==='no-observation')delete s.snapshot.status.jobs[40003].result.native_behavior_observed;
          if(failure==='claims-readback')s.snapshot.status.jobs[40003].result.assignment_read_back=true;
          if(failure==='wrong-original')s.snapshot.status.jobs[40003].result.original_assignment=300;
          if(failure==='publication-pending')Object.assign(s.snapshot.status.jobs[40003].result,{ready:false,publication_pending:true,requires_review:true});
        }
        await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
      }
      assert(await p.evaluate(()=>accessoryRequests.get(40003).review));
      await p.evaluate(()=>openAnyplaceSetup(40003));
      assert.equal(await p.getByRole('button',{name:'Start experimental setup',exact:true}).count(),0);
      assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
      if(failure==='publication-pending')assert(await p.getByText(/Observed assignment behavior was saved, but the publication refresh needs review/).isVisible());
    });
  }
  await scenario('anyplace-accepted-job-survives-refresh-failure',async(p,s)=>{
    await anyplace(p,s);
    s.handlers['/api/controller/setup']=({send})=>{s.snapshotFail=true;return send({accepted:true,completed:false,job_id:'anyplace-setup-one'},202);};
    await beginAnyplace(p);await p.getByText(/Experimental setup accepted/).waitFor();
    assert(await p.evaluate(()=>accessoryRequests.get(40003).pending&&accessoryRequests.get(40003).job==='anyplace-setup-one'));
    await p.evaluate(()=>openAnyplaceSetup(40003));
    assert.equal(await p.getByRole('button',{name:'Start experimental setup',exact:true}).count(),0);
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
  });
  await scenario('anyplace-received-busy-refusal-requires-another-review',async(p,s)=>{
    await anyplace(p,s);
    s.handlers['/api/controller/setup']=({send})=>send({error:'mesh_busy',operation_started:false,requires_review:false,retry_allowed:true},409);
    await beginAnyplace(p);await p.getByText(/Setup did not start/).waitFor();
    assert(!await p.evaluate(()=>accessoryRequests.get(40003).review));
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
    await p.getByRole('button',{name:'Set up experimental HA control',exact:true}).click();
    assert(await p.getByRole('button',{name:'Start experimental setup',exact:true}).isVisible());
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
  });
  await scenario('anyplace-no-dispatch-cancellation-needs-new-manual-review',async(p,s)=>{
    await anyplace(p,s);
    let attempts=0;
    s.handlers['/api/controller/setup']=({body,send})=>{assert.deepEqual(body,{target:40003});attempts++;
      return send({accepted:true,completed:false,job_id:'anyplace-setup-'+attempts},202);};
    await beginAnyplace(p);await p.getByText(/Experimental setup accepted/).waitFor();
    s.snapshot.status.jobs[40003]={id:'anyplace-setup-1',state:'not_started',error:'anyplace_wake_timeout',
      operation_started:false,requires_review:false,retry_allowed:false,message:'The wake timed out before assignment dispatch.'};
    s.snapshot.status.anyplace_progress[40003]='needs_review';
    s.snapshot.inventory.groups.push({group_id:24573,name:'Abandoned HA target',members:[],protected:true,exposed:false});
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/Setup did not start. The wake timed out before assignment dispatch/).waitFor();
    assert(!await p.evaluate(()=>accessoryRequests.get(40003).review));
    assert(await p.getByRole('button',{name:'Set up experimental HA control',exact:true}).isEnabled());
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert.equal(attempts,1,'a proven cancellation must never automatically create another request');
    await p.getByRole('button',{name:'Set up experimental HA control',exact:true}).click();
    assert.equal(attempts,1,'opening a new review is not permission to dispatch');
    await p.getByRole('button',{name:'Start experimental setup',exact:true}).click();
    await p.getByText(/Experimental setup accepted/).waitFor();
    assert.equal(attempts,2);
    finishAnyplace(s);
    s.snapshot.status.jobs[40003].id='anyplace-setup-2';
    s.snapshot.status.jobs[40003].result.transaction='new-anyplace-tx';
    Object.assign(s.snapshot.controllers[0].anyplace_ha,{transaction:'new-anyplace-tx',target:24572});
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/Experimental setup recorded:/).waitFor();
    assert.equal(attempts,2);
    assert.equal(await p.evaluate(()=>grp(24573).protected),true,'abandoned targets stay reserved');
  });
  await scenario('anyplace-cancelled-without-no-dispatch-proof-stays-blocked',async(p,s)=>{
    await anyplace(p,s);
    s.handlers['/api/controller/setup']=({send})=>send({accepted:true,completed:false,job_id:'anyplace-setup-one'},202);
    await beginAnyplace(p);await p.getByText(/Experimental setup accepted/).waitFor();
    s.snapshot.status.jobs[40003]={id:'anyplace-setup-one',state:'not_started',error:'anyplace_admission_expired',
      operation_started:false,requires_review:true,message:'The cancelled receipt needs review.'};
    s.snapshot.controllers[0].anyplace_ha.state='needs_review';
    s.snapshot.controllers[0].actions.setup_ha={enabled:false,reason:'fresh_retained_claim_required'};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(await p.evaluate(()=>accessoryRequests.get(40003).review));
    await p.evaluate(()=>openAnyplaceSetup(40003));
    assert.equal(await p.getByRole('button',{name:'Start experimental setup',exact:true}).count(),0);
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
  });
  await scenario('anyplace-review-rechecks-session-and-mqtt',async(p,s)=>{
    await anyplace(p,s);await p.getByRole('button',{name:'Set up experimental HA control',exact:true}).click();
    s.snapshot.status.instance='two';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByRole('button',{name:'Start experimental setup',exact:true}).click();
    assert(!s.calls.some(c=>c.path==='/api/controller/setup'));
    await p.getByRole('button',{name:'Set up experimental HA control',exact:true}).click();
    s.snapshot.status.mqtt_ready=false;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByRole('button',{name:'Start experimental setup',exact:true}).click();
    assert(!s.calls.some(c=>c.path==='/api/controller/setup'));
  });
  await scenario('anyplace-retained-review-blocks-setup-after-page-reload',async(p,s)=>{
    await anyplace(p,s);
    s.snapshot.controllers[0].anyplace_ha.state='needs_review';
    s.snapshot.controllers[0].actions.setup_ha={enabled:false,reason:'fresh_retained_claim_required'};
    await p.reload();await p.waitForFunction(()=>S.synced);
    await p.evaluate(()=>openDrawer('device',40003));
    await p.getByText(/Previous Anyplace setup needs review/).waitFor();
    assert.equal(await p.getByRole('button',{name:'Set up experimental HA control',exact:true}).count(),0);
    await p.evaluate(()=>openAnyplaceSetup(40003));
    assert(!s.calls.some(c=>c.path==='/api/controller/setup'));
  });
  await scenario('pairing-controller-finish-uses-open-instead-of-flash',async(p,s)=>{
    await anyplace(p,s);await p.evaluate(()=>closeDrawer());
    await p.evaluate(()=>{S.wiz=newWizard();S.wiz.step=3;S.wiz.claims=[{id:40003,type:81,st:'done',name:'Anyplace'}];go('add');});
    assert.equal(await p.getByRole('button',{name:'Flash',exact:true}).count(),0);
    assert(await p.getByRole('button',{name:'Open controller',exact:true}).isVisible());
    assert.equal(await p.getByText(/look up/).count(),0);
    await p.getByRole('button',{name:'Open controller',exact:true}).click();
    await p.getByText('Anyplace dimmer control',{exact:true}).waitFor();
    await p.evaluate(()=>{closeDrawer();S.wiz.claims.push({id:40001,type:93,st:'done',name:'Light'});render();});
    assert.equal(await p.getByRole('button',{name:'Flash',exact:true}).count(),1);
    assert.equal(await p.getByRole('button',{name:'Open controller',exact:true}).count(),1);
    assert(!s.calls.some(c=>c.path==='/api/control'||c.path==='/api/controller/setup'));
  });
  await scenario('anyplace-pairing-prompt-matches-hash-and-instance',async(p,s)=>{
    await p.evaluate(()=>{S.wiz=newWizard();S.wiz.step=2;S.wiz.claims=[{type:81,st:'run',uuid_hash:123,instance:'one',msg:'Pairing in progress'}];go('add');});
    s.snapshot.status.claim_progress={40003:{uuid_hash:456,phase:'waiting_for_wake'}};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert.equal(await p.getByText(/Keys exchanged. Press the small Wake-Up button/).count(),0);
    s.snapshot.status.claim_progress[40003].uuid_hash=123;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/Keys exchanged. Press the small Wake-Up button/).waitFor();
    s.snapshot.status.claim_progress[40003].phase='verifying';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/Wake received. Checking the encrypted pairing reply/).waitFor();
    s.snapshot.status.instance='two';s.snapshot.status.claim_progress[40003].phase='waiting_for_wake';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert.equal(await p.getByText(/Keys exchanged. Press the small Wake-Up button/).count(),0);
    assert(await p.getByText('Pairing in progress',{exact:true}).isVisible());
    assert(!s.calls.some(c=>c.path==='/api/claim'));
  });
  const battery = async(p,s,enabled=true) => {
    Object.assign(s.snapshot.status,{mqtt_enabled:true,mqtt_ready:true,keypad_progress:{}});
    Object.assign(s.snapshot.inventory.devices[2],{product_id:126,name:'Battery keypad'});
    s.snapshot.controllers[0]={device_id:40003,product_id:126,ha_status:'not_exposed',
      assignment_read_back:false,revision:null,slots:[],last_event:null,keypad_ha_setup:null,
      actions:{read_assignments:{enabled:true},edit_assignment:{enabled:false},
        setup_ha:{enabled,reason:enabled?'available':'model_setup_disabled'}}};
    await p.evaluate(snapshot=>{applySnapshot(snapshot);openDrawer('device',40003);},clone(s.snapshot));
    await p.getByRole('button',{name:'Set up unused buttons for HA',exact:true}).waitFor();
  };
  await scenario('battery-observer-tab-and-reload-see-owned-job-prompts',async(p,s)=>{
    await battery(p,s,false);s.snapshot.controllers[0].editor_available=true;
    s.snapshot.controllers[0].actions.read_assignments={enabled:false,reason:'operation_in_progress'};
    for(const operation of ['keypad_read','keypad_save','keypad_setup']){
      const id='observed-'+operation;
      s.snapshot.status.jobs[40003]={id,state:'running',progress:{phase:'waiting_for_wake',job_id:id,target:40003,product_id:126,operation,instance:'one'}};
      s.snapshot.status.keypad_progress[40003]='reading'; // unrelated plain cache is not authoritative
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
      await p.getByText('Press the bottom Power/Off button once now to wake the battery keypad.',{exact:true}).waitFor();
      assert(await p.evaluate(()=>!controllerReads.size&&!keypadSaves.size&&!batterySetups.size));
    }
    await p.reload();await p.waitForFunction(()=>S.synced);await p.evaluate(()=>openDrawer('device',40003));
    await p.getByText('Press the bottom Power/Off button once now to wake the battery keypad.',{exact:true}).waitFor();
    assert(await p.getByRole('button',{name:'Read button assignments',exact:true}).isDisabled());
    await p.getByText(/Automatic setup of unused buttons is disabled. Individual button editing is enabled/).waitFor();
    await p.screenshot({path:path.join(evidence,'battery-observer-wake-mobile.png'),fullPage:true});
    for(const [phase,text] of [['cooldown','Wait before the next wake.'],['settling','Wake received.'],['reading','Reading the keypad.']]){
      s.snapshot.status.jobs[40003].progress.phase=phase;
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.getByText(text,{exact:false}).waitFor();
    }
    assert(!s.calls.some(c=>['/api/keypad/read','/api/keypad/save','/api/controller/setup'].includes(c.path)));
    await p.screenshot({path:path.join(evidence,'battery-observer-progress-mobile.png'),fullPage:true});
  },true);
  await scenario('battery-observer-rejects-stale-or-unrelated-job-progress',async(p,s)=>{
    await battery(p,s);
    const valid={id:'active-read',state:'running',progress:{phase:'waiting_for_wake',job_id:'active-read',target:40003,product_id:126,operation:'keypad_read',instance:'one'}};
    for(const defect of ['job','target','model','operation','instance','completed','null']){
      const job=clone(valid);
      if(defect==='job')job.progress.job_id='previous';
      if(defect==='target')job.progress.target=40004;
      if(defect==='model')job.progress.product_id=127;
      if(defect==='operation')job.progress.operation='unrelated';
      if(defect==='instance')job.progress.instance='previous';
      if(defect==='completed')job.state='completed';
      if(defect==='null')job.progress=null;
      s.snapshot.status.jobs[40003]=job;s.snapshot.status.keypad_progress[40003]='waiting_for_wake';
      await p.evaluate(snapshot=>{controllerReads.set(40003,{job:'active-read',instance:'one',pending:true});applySnapshot(snapshot);},clone(s.snapshot));
      await p.waitForTimeout(180);
      assert.equal(await p.getByText('Press the bottom Power/Off button once now to wake the battery keypad.',{exact:true}).count(),0,defect);
    }
    assert(!s.calls.some(c=>['/api/keypad/read','/api/keypad/save','/api/controller/setup'].includes(c.path)));
  });
  const beginBattery = async p => {
    await p.getByRole('button',{name:'Set up unused buttons for HA',exact:true}).click();
    await p.getByRole('button',{name:'Start battery setup',exact:true}).click();
  };
  const finishBattery = (s,{transaction='battery-tx',proof='battery-tx',ready=true,changed=true}={}) => {
    Object.assign(s.snapshot.status,{pending_transactions:0,keypad_progress:{40003:'verified'}});
    s.snapshot.status.jobs[40003]={id:'battery-one',state:'completed',result:changed
      ? {transaction,setup_complete:true,ready,existing_assignments_preserved:true}
      : {changed:false,table:{revision:'battery-table'},ready}};
    Object.assign(s.snapshot.controllers[0],{assignment_read_back:true,revision:'battery-table',
      keypad_ha_setup:changed?{transaction:proof,revision:'battery-table',slots:[1]}:null,
      slots:[{slot:1,target:changed?24575:301,preset:255,status:'verified_mapping',ha_owned:changed,
        assignment_mode:changed?'ha':'native',editable:false,ha_available:false},
        ...[2,3,4].map(slot=>({slot,target:300+slot,preset:255,status:'verified_mapping',assignment_mode:'native',editable:true,ha_available:false}))]});
  };
  await scenario('battery-setup-requires-model-gate-and-mqtt',async(p,s)=>{
    await battery(p,s,false);
    assert(await p.getByRole('button',{name:'Set up unused buttons for HA',exact:true}).isDisabled());
    s.snapshot.controllers[0].actions.setup_ha.enabled=true;s.snapshot.status.mqtt_ready=false;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(await p.getByRole('button',{name:'Set up unused buttons for HA',exact:true}).isDisabled());
    await p.evaluate(()=>openBatterySetup(40003));assert(!s.calls.some(c=>c.path==='/api/controller/setup'));
    assert(await p.getByText(/Read the buttons to choose Direct group, Home Assistant or Unused/).isVisible());
  });
  await scenario('battery-read-wake-phases-and-restart-recovery',async(p,s)=>{
    await battery(p,s);
    await p.getByRole('button',{name:'Read button assignments',exact:true}).click();
    await p.getByText('Read accepted. Waiting for both table reads.',{exact:true}).waitFor();
    s.snapshot.status.jobs[40003]={id:'foreign-read',state:'running'};
    s.snapshot.status.keypad_progress[40003]='waiting_for_wake';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert.equal(await p.getByText('Press the bottom Power/Off button once now to wake the battery keypad.',{exact:true}).count(),0);
    s.snapshot.status.jobs[40003].id='read-one';
    for(const [phase,copy] of [['waiting_for_wake','Press the bottom Power/Off button once now'],['cooldown','Wait before the next wake.'],['settling','Wake received.'],['reading','Reading the keypad.']]){
      s.snapshot.status.keypad_progress[40003]=phase;
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
      await p.getByText(copy,{exact:false}).waitFor();
    }
    await p.evaluate(()=>window.readButton=document.querySelector('button[onclick="readController(40003)"]'));
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(await p.evaluate(()=>window.readButton===document.querySelector('button[onclick="readController(40003)"]')),'identical phase must preserve controls');
    s.snapshot.status.instance='two';s.snapshot.status.jobs={};
    s.snapshot.controllers[0].actions.read_assignments={enabled:false,reason:'resolve_pending_operations'};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/bridge restarted during the read/).waitFor();
    assert(await p.getByRole('button',{name:'Read button assignments',exact:true}).isDisabled());
    assert.equal(s.calls.filter(c=>c.path==='/api/keypad/read').length,1);
    s.snapshot.controllers[0].actions.read_assignments.enabled=true;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    await p.getByRole('button',{name:'Read button assignments',exact:true}).click();
    await p.getByText('Read accepted. Waiting for both table reads.',{exact:true}).waitFor();
    assert.equal(s.calls.filter(c=>c.path==='/api/keypad/read').length,2);
    assert(!s.calls.some(c=>c.path==='/api/controller/setup'));
  });
  await scenario('battery-setup-retains-job-and-preserved-native-buttons',async(p,s)=>{
    await battery(p,s);
    s.handlers['/api/controller/setup']=({body,send})=>{assert.deepEqual(body,{target:40003});s.snapshotFail=true;return send({accepted:true,completed:false,job_id:'battery-one'},202);};
    await beginBattery(p);await p.getByText(/Battery setup accepted/).waitFor();
    await p.evaluate(()=>{openBatterySetup(40003);confirmBatterySetup();});
    assert(await p.evaluate(()=>batterySetups.get(40003).pending&&batterySetups.get(40003).job==='battery-one'));
    finishBattery(s);s.snapshot.status.jobs[40003].id='unrelated-job';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(await p.evaluate(()=>batterySetups.get(40003).pending));
    s.snapshot.status.jobs[40003].id='battery-one';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/Unused buttons were configured and read back; existing assignments were preserved. 1 button has verified HA targets./).waitFor();
    assert(await p.getByRole('button',{name:'Set up unused buttons for HA',exact:true}).isDisabled());
    assert(!await p.evaluate(()=>batterySetups.get(40003).review));
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
    await p.screenshot({path:path.join(evidence,'battery-preserved-native-completion.png'),fullPage:true});
  });
  for(const mode of ['wrong-proof','partial-readiness','restart','lost-response']){
    await scenario('battery-setup-blocks-replay-'+mode,async(p,s)=>{
      await battery(p,s);
      s.handlers['/api/controller/setup']=({send})=>mode==='lost-response'
        ? send({error:'response_lost'},504):send({accepted:true,completed:false,job_id:'battery-one'},202);
      await beginBattery(p);
      if(mode==='lost-response')await p.getByText(/Battery setup outcome is not confirmed/).waitFor();
      else{
        await p.getByText(/Battery setup accepted/).waitFor();
        if(mode==='restart')s.snapshot.status.instance='two';
        else finishBattery(s,{proof:mode==='wrong-proof'?'other-transaction':'battery-tx',ready:mode!=='partial-readiness'});
        await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
      }
      assert(await p.evaluate(()=>batterySetups.get(40003).review));
      assert(await p.getByRole('button',{name:'Set up unused buttons for HA',exact:true}).isDisabled());
      await p.evaluate(()=>openBatterySetup(40003));
      assert.equal(await p.getByRole('button',{name:'Start battery setup',exact:true}).count(),0);
      assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
      if(mode==='partial-readiness')assert(await p.getByText(/HA event publication is not ready/).isVisible());
    });
  }
  await scenario('battery-received-busy-refusal-never-auto-retries',async(p,s)=>{
    await battery(p,s);
    s.handlers['/api/controller/setup']=({send})=>send({error:'mesh_busy',operation_started:false,requires_review:false,retry_allowed:true,message:'Bridge busy'},409);
    await beginBattery(p);await p.getByText(/Battery setup did not start/).waitFor();
    assert(!await p.evaluate(()=>batterySetups.get(40003).review));
    assert(await p.getByRole('button',{name:'Set up unused buttons for HA',exact:true}).isEnabled());
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
    await p.getByRole('button',{name:'Set up unused buttons for HA',exact:true}).click();
    assert(await p.getByRole('button',{name:'Start battery setup',exact:true}).isVisible());
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
  });
  await scenario('battery-no-op-preserves-existing-native-controls',async(p,s)=>{
    await battery(p,s);
    s.handlers['/api/controller/setup']=({send})=>send({accepted:true,completed:false,job_id:'battery-one'},202);
    await beginBattery(p);await p.getByText(/Battery setup accepted/).waitFor();
    finishBattery(s,{changed:false,ready:false});
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/No unused buttons needed setup. Existing assignments were preserved./).waitFor();
    assert(await p.getByText(/HA event publication is not ready/).isVisible());
    assert.equal(await p.getByText(/verified HA targets/).count(),0);
    assert(await p.getByRole('button',{name:'Set up unused buttons for HA',exact:true}).isDisabled());
  });
  await scenario('controller-setup-local-admission-cross-device-and-model',async(p,s)=>{
    await battery(p,s);
    assert(await p.evaluate(()=>batterySetupAllowed(40003)));
    await p.evaluate(()=>batterySetups.set(40004,{pending:true}));
    assert(!await p.evaluate(()=>batterySetupAllowed(40003)));
    await p.evaluate(()=>{batterySetups.clear();accessoryRequests.set(40004,{pending:true});});
    assert(!await p.evaluate(()=>batterySetupAllowed(40003)));
    await p.evaluate(()=>accessoryRequests.clear());
    await accessory(p,s);
    assert(await p.evaluate(()=>accessoryAllowed(40003,'setup_ha')));
    await p.evaluate(()=>batterySetups.set(40004,{pending:true}));
    assert(!await p.evaluate(()=>accessoryAllowed(40003,'setup_ha')));
    assert(!s.calls.some(c=>c.path==='/api/controller/setup'));
  });
  await scenario('anyplace-only-gated-native-review-and-explicit-acceptance',async(p,s)=>{
    Object.assign(s.snapshot.inventory.devices[2],{product_id:81,name:'Anyplace dimmer'});
    s.snapshot.controllers[0]={device_id:40003,product_id:81,ha_status:'disabled',assignment_read_back:false,slots:[],
      actions:{setup_ha:{enabled:true},read_assignments:{enabled:false},assign_native_target:{enabled:false,reason:'model_editor_disabled'}}};
    await p.evaluate(snapshot=>{applySnapshot(snapshot);openDrawer('device',40003);},clone(s.snapshot));
    await p.getByText('Anyplace dimmer control',{exact:true}).waitFor();
    await p.getByText('Direct group control',{exact:true}).click();
    assert(await p.getByRole('button',{name:'Choose direct group',exact:true}).isDisabled());
    assert.equal(await p.getByRole('button',{name:'Set up Home Assistant control',exact:true}).count(),0);
    assert.equal(await p.getByRole('button',{name:'Read button assignments',exact:true}).count(),0);
    await p.evaluate(()=>openAccessorySetup(40003));
    assert.equal(await p.getByRole('button',{name:'Start HA setup',exact:true}).count(),0);
    s.snapshot.controllers[0].actions.assign_native_target={enabled:true,reason:'available'};
    s.snapshot.controllers[0].observed_target={target:301,valid_for_ms:15000,baseline:{target:301,observed_ms:100,target_generation:1}};
    await p.evaluate(snapshot=>{applySnapshot(snapshot);openAccessoryNative(40003);},clone(s.snapshot));
    await p.locator('#accessoryGroup').selectOption('300');
    await p.evaluate(()=>accessoryReview.deadline=Date.now()-1);
    await p.getByRole('button',{name:'Assign group',exact:true}).click();
    assert(!s.calls.some(c=>c.path==='/api/dimmer/assign'));
    await p.evaluate(()=>openAccessoryNative(40003));await p.locator('#accessoryGroup').selectOption('300');
    s.handlers['/api/dimmer/assign']=({body,send})=>{assert.deepEqual(body,{target:40003,group:300,baseline:{target:301,observed_ms:100,target_generation:1}});return send({accepted:true,completed:false,job_id:'anyplace-one'},202);};
    await p.getByRole('button',{name:'Assign group',exact:true}).click();
    await p.getByText(/Wake the Anyplace dimmer now/).waitFor();
    s.snapshot.status.jobs[40003]={id:'anyplace-one',state:'completed',result:{acceptance_required:true}};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/Complete the explicit acceptance in Operations/).waitFor();
    assert(await p.evaluate(()=>accessoryRequests.get(40003).review));
    assert.equal(s.calls.filter(c=>c.path==='/api/dimmer/assign').length,1);
    assert(!s.calls.some(c=>c.path==='/api/controller/setup'));
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await p.screenshot({path:path.join(evidence,'anyplace-mobile-review.png'),fullPage:true});
  },true);
  for (const mobile of [false,true]) await scenario('home-groups-first-room-guidance-'+(mobile?'phone':'desktop'),async(p,s)=>{
    s.snapshot.device_rooms={'40001':'Theatre','40002':'Theatre','40003':'Theatre'};
    await p.evaluate(snapshot=>{applySnapshot(snapshot);go('home');},clone(s.snapshot));
    assert.deepEqual(await p.locator('.home-section').evaluateAll(nodes=>nodes.map(n=>n.getAttribute('aria-labelledby'))),['homeGroups','homeLights','homeControllers']);
    await p.getByText('For the best response when controlling several lights together, use a group.',{exact:true}).waitFor();
    await p.getByText(/Adding a light to a room does not add it to a group/).waitFor();
    await p.getByText(/Room controls send to each light separately/).waitFor();
    assert.equal(s.snapshot.inventory.groups[0].members.length,0,'room assignment never creates membership');
    s.snapshot.inventory.groups[0].members=[40001,40002];
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText('Room controls use the Theatre group.',{exact:true}).waitFor();
    assert.equal(await p.getByText(/Room controls send to each light separately/).count(),0);
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await p.screenshot({path:path.join(evidence,'home-groups-first-'+(mobile?'phone':'desktop')+'.png'),fullPage:true});
    await p.evaluate(()=>openDrawer('device',40001));
    await p.getByText(/Assigning a room does not add lights to a group or change wall controls/).waitFor();
    assert(!s.calls.some(c=>c.body),'room/group guidance sends no writes');
  },mobile);
  await scenario('groups-hide-internal-targets-without-changing-controller-state',async(p,s)=>{
    await keypad(p,s);
    s.snapshot.inventory.groups.push({group_id:302,name:'HA keypad target: my lighting group',members:[],protected:false},
      {group_id:24574,name:'Internal dimmer input',members:[],protected:true},
      {group_id:24566,name:'All lights reserved target',members:[],protected:false});
    const before=clone(s.snapshot);
    await p.evaluate(snapshot=>{applySnapshot(snapshot);go('groups');},clone(s.snapshot));
    assert.deepEqual(await p.locator('tbody tr td:first-child').getByRole('button').allTextContents(),['Theatre','Bar','HA keypad target: my lighting group']);
    assert.equal(await p.locator('tbody').getByText('HA keypad target',{exact:true}).count(),0);
    assert.equal(await p.locator('tbody').getByText('Internal dimmer input',{exact:true}).count(),0);
    assert.equal(await p.locator('tbody').getByText('All lights reserved target',{exact:true}).count(),0);
    await p.screenshot({path:path.join(evidence,'lighting-groups-only.png'),fullPage:true});
    await p.evaluate(()=>go('home'));
    assert.equal(await p.locator('#homeGroups .badge').innerText(),'3');
    assert.equal(await p.locator('.attention').getByText('"HA keypad target" has no lights',{exact:true}).count(),0);
    assert.equal(await p.locator('.attention').getByText('"All lights reserved target" has no lights',{exact:true}).count(),0);
    await p.evaluate(()=>go('settings'));
    assert((await p.locator('body').innerText()).includes('2 lights, 1 controllers, 3 groups'));
    await p.evaluate(()=>openDrawer('device',40003));
    assert(await p.getByText('Protected or unknown assignment · HA keypad target (cannot be changed here)',{exact:true}).isVisible());
    assert.equal(await p.locator('#kpG1 option[value="24575"]').count(),0);
    assert.equal(await p.locator('#kpG1 option[value="24574"]').count(),0);
    assert.equal(await p.locator('#kpG1 option[value="302"]').count(),1,'ordinary group name must not be filtered');
    assert.equal(await p.evaluate(()=>grp(24575).protected),true,'reservation stays available for controller lookups');
    assert.deepEqual(s.snapshot,before);assert(!s.calls.some(c=>c.body),'view changes send no writes');
  });
  await scenario('only-internal-targets-show-empty-lighting-groups',async(p,s)=>{
    s.snapshot.inventory.groups=[{group_id:24575,name:'HA keypad target: 40003/1',members:[],protected:true}];
    await p.evaluate(snapshot=>{applySnapshot(snapshot);go('groups');},clone(s.snapshot));
    await p.getByText('No lighting groups yet.',{exact:true}).waitFor();
    assert.equal(await p.locator('tbody tr[onclick]').count(),0);
    await p.evaluate(()=>go('home'));
    assert.equal(await p.locator('#homeGroups .badge').innerText(),'0');
    assert(await p.getByText('No lighting groups yet. Create one from Groups.',{exact:true}).isVisible());
    assert.equal(await p.evaluate(()=>S.groups.length),1);
    assert(!s.calls.some(c=>c.body));
  });
  await scenario('accessory-disabled-until-capability-and-mqtt',async(p,s)=>{
    await accessory(p,s,false);
    assert(await p.getByRole('button',{name:'Set up Home Assistant control',exact:true}).isDisabled());
    s.snapshot.controllers[0].actions.setup_ha={enabled:true,reason:'available'};
    s.snapshot.status.mqtt_ready=false;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(await p.getByRole('button',{name:'Set up Home Assistant control',exact:true}).isDisabled());
    assert(!s.calls.some(c=>c.path==='/api/controller/setup'));
  });
  await scenario('accessory-setup-progress-and-owned-completion',async(p,s)=>{
    await accessory(p,s);
    s.handlers['/api/controller/setup']=({body,send})=>{assert.deepEqual(body,{target:40003});pendingAccessory(s);return send({accepted:true,completed:false,job_id:'acc-one'},202);};
    await beginAccessory(p);
    await p.getByText(/Setup accepted\. Wait for the button prompt/).waitFor();
    await p.evaluate(()=>confirmAccessorySetup());
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
    s.snapshot.status.pending_transactions=0;
    s.snapshot.status.jobs[40003]={id:'old-job',state:'completed',result:{ready:true,transaction:'old'}};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(await p.evaluate(()=>accessoryRequests.get(40003).pending));
    s.snapshot.status.jobs[40003]={id:'acc-one',state:'completed',result:{ready:true,transaction:'acc-tx'}};
    s.snapshot.controllers[0].accessory_ha={state:'configured',target:24574,transaction:'acc-tx',observed_in_session:true};
    s.snapshot.controllers[0].ha_status='publishing_enabled';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/HA setup completed:/).waitFor();
    assert(!await p.evaluate(()=>accessoryRequests.get(40003).review));
    assert.equal(await p.getByRole('button',{name:'Set up Home Assistant control',exact:true}).count(),0);
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
  });
  await scenario('accessory-accepted-job-survives-refresh-failure',async(p,s)=>{
    await accessory(p,s);
    s.handlers['/api/controller/setup']=({send})=>{s.snapshotFail=true;return send({accepted:true,completed:false,job_id:'acc-one'},202);};
    await beginAccessory(p);await p.getByText(/Setup accepted\. Wait for the button prompt/).waitFor();
    assert(await p.evaluate(()=>accessoryRequests.get(40003).pending&&accessoryRequests.get(40003).job==='acc-one'));
    await p.evaluate(()=>openAccessorySetup(40003));
    assert.equal(await p.getByRole('button',{name:'Start HA setup',exact:true}).count(),0);
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
  });
  await scenario('accessory-uncertain-write-never-retries',async(p,s)=>{
    await accessory(p,s);
    s.handlers['/api/controller/setup']=({send})=>send({error:'timeout_outcome_unknown'},504);
    await beginAccessory(p);await p.getByText(/Setup outcome is not confirmed/).waitFor();
    await p.evaluate(()=>openAccessorySetup(40003));
    assert(await p.evaluate(()=>accessoryRequests.get(40003).review));
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
  });
  await scenario('accessory-modal-rechecks-mqtt-and-session',async(p,s)=>{
    await accessory(p,s);await p.getByRole('button',{name:'Set up Home Assistant control',exact:true}).click();
    s.snapshot.status.instance='two';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByRole('button',{name:'Start HA setup',exact:true}).click();
    assert(!s.calls.some(c=>c.path==='/api/controller/setup'));
  });
  await scenario('accessory-restart-keeps-uncertain-operation-blocked',async(p,s)=>{
    await accessory(p,s);
    s.handlers['/api/controller/setup']=({send})=>send({accepted:true,completed:false,job_id:'acc-one'},202);
    await beginAccessory(p);await p.getByText(/Setup accepted\. Wait for the button prompt/).waitFor();
    s.snapshot.status.instance='two';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/bridge restarted during setup/).waitFor();
    assert(await p.evaluate(()=>accessoryRequests.get(40003).review));
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup').length,1);
  });
  await scenario('accessory-native-review-expiry-and-baseline-change',async(p,s)=>{
    await accessory(p,s);
    s.snapshot.controllers[0].actions.assign_native_target={enabled:true,reason:'available'};
    s.snapshot.controllers[0].observed_target={target:301,valid_for_ms:15000,baseline:{target:301,observed_ms:100,target_generation:1}};
    await p.evaluate(snapshot=>{applySnapshot(snapshot);openAccessoryNative(40003);},clone(s.snapshot));
    await p.locator('#accessoryGroup').selectOption('300');
    await p.evaluate(()=>accessoryReview.deadline=Date.now()-1);
    await p.getByRole('button',{name:'Assign group',exact:true}).click();
    assert(!s.calls.some(c=>c.path==='/api/dimmer/assign'));
    await p.evaluate(()=>openAccessoryNative(40003));await p.locator('#accessoryGroup').selectOption('300');
    s.snapshot.controllers[0].observed_target.baseline.target_generation=2;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByRole('button',{name:'Assign group',exact:true}).click();
    assert(!s.calls.some(c=>c.path==='/api/dimmer/assign'));
    await p.evaluate(()=>openAccessoryNative(40003));await p.locator('#accessoryGroup').selectOption('300');
    s.handlers['/api/dimmer/assign']=({body,send})=>{assert.deepEqual(body,{target:40003,group:300,baseline:{target:301,observed_ms:100,target_generation:2}});return send({accepted:true,completed:false,job_id:'native-one'},202);};
    await p.getByRole('button',{name:'Assign group',exact:true}).click();
    await p.getByText(/Waiting for the dimmer\. Press On/).waitFor();
    assert.equal(s.calls.filter(c=>c.path==='/api/dimmer/assign').length,1);
  });
  await scenario('accessory-mobile-configured-event-display',async(p,s)=>{
    await accessory(p,s);
    s.snapshot.controllers[0].accessory_ha={state:'configured',target:24574,transaction:'acc-tx',observed_in_session:false};
    s.snapshot.controllers[0].last_event={event_type:'brightness',brightness:128,target:24574,age_ms:1000};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText('Home Assistant target configured',{exact:true}).waitFor();
    await p.getByText(/Last observed input at .+brightness/).waitFor();
    assert(await p.getByText(/Press the dimmer to check input after reconnect/).count());
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await p.screenshot({path:path.join(evidence,'accessory-mobile.png'),fullPage:true});
  },true);
  await scenario('keypad-dropdown-survives-live-snapshots',async(p,s)=>{
    await keypad(p,s);
    await p.locator('#kpM1').focus();
    await p.locator('#kpM1').press('Space');
    await p.evaluate(()=>window.openButtonSelect=document.querySelector('#kpM1'));
    for(let age=1000;age<=3000;age+=1000){
      s.snapshot.controllers[0].last_event={event_type:'on',button:1,target:300,brightness:200,age_ms:age};
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
      await p.waitForTimeout(180);
      assert(await p.evaluate(()=>window.openButtonSelect===document.querySelector('#kpM1')),'Live snapshot must preserve the open dropdown');
    }
    await p.keyboard.press('Escape');
    await p.getByLabel('Button 1 control',{exact:true}).selectOption('ha');
    await p.locator('#kpG1').waitFor({state:'detached'});
    assert(await p.getByRole('button',{name:'Save 1 change',exact:true}).isEnabled());
    await p.getByLabel('Button 1 control',{exact:true}).selectOption('native');
    await p.locator('#kpG1').waitFor();
    await p.locator('#kpG1').focus();
    await p.evaluate(()=>window.openGroupSelect=document.querySelector('#kpG1'));
    s.snapshot.controllers[0].last_event.age_ms=4000;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(await p.evaluate(()=>window.openGroupSelect===document.querySelector('#kpG1')));
    await p.getByLabel('Button 1 group',{exact:true}).selectOption('301');
    await p.getByRole('button',{name:'Save 1 change',exact:true}).waitFor();
    assert.deepEqual(await p.evaluate(()=>keypadChanges(40003)),[{slot:1,mode:'native',group:301,brightness:200}]);
    assert(!s.calls.some(c=>c.path==='/api/keypad/save'));
  });
  for (const model of [127,126]) await scenario(`keypad-mobile-picker-no-repeat-${model}`,async(p,s)=>{
    await keypad(p,s);
    s.snapshot.controllers[0].product_id=model;
    s.snapshot.inventory.devices[2].product_id=model;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.waitForTimeout(180);
    // Track application focus calls: emulation cannot show the iOS system
    // picker, but must not recreate/reopen it by focusing a selected field.
    await p.evaluate(()=>{
      window.selectFocusCalls=[];
      const original=HTMLElement.prototype.focus;
      HTMLElement.prototype.focus=function(...args){
        if(this.matches('#drawerRoot select'))window.selectFocusCalls.push(this.id);
        return original.apply(this,args);
      };
    });
    async function choose(id,value){
      const select=p.locator('#'+id);
      await select.tap();
      await p.evaluate(()=>window.selectFocusCalls=[]);
      const original=await select.elementHandle();
      s.snapshot.controllers[0].last_event={event_type:'on',button:1,target:300,age_ms:1000};
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
      await p.waitForTimeout(180);
      assert(await original.evaluate(el=>el===document.getElementById(el.id)),'open picker node must survive snapshots');
      assert.deepEqual(await p.evaluate(()=>window.selectFocusCalls),[],'background update must not refocus the open picker');
      await select.selectOption(value);
      await p.waitForTimeout(180);
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
      await p.waitForTimeout(180);
      assert.equal(await p.locator('#'+id).inputValue(),value);
      assert.deepEqual(await p.evaluate(()=>window.selectFocusCalls),[],'completed touch selection must not be focused again');
      assert(await p.evaluate(()=>activeNativeSelect===null),'completed touch selection must release the drawer');
    }
    await choose('kpM1','ha');
    await p.locator('#kpG1').waitFor({state:'detached'});
    await choose('kpM1','native');
    await choose('kpG1','301');
    await choose('kpL1','128');
    assert.deepEqual(await p.evaluate(()=>keypadChanges(40003)),[{slot:1,mode:'native',group:301,brightness:128}]);
    assert(await p.getByRole('button',{name:'Save 1 change',exact:true}).isEnabled());
    assert(!s.calls.some(c=>c.body),'choosing a draft must never program a controller');
    await p.screenshot({path:path.join(evidence,`keypad-mobile-picker-${model}.png`),fullPage:true});
  },true);
  await scenario('keypad-keyboard-selection-keeps-navigation',async(p,s)=>{
    await keypad(p,s);
    await p.locator('#kpM1').focus();
    await p.locator('#kpM1').press('ArrowDown');
    await p.locator('#kpM1').selectOption('ha');
    await p.locator('#kpG1').waitFor({state:'detached'});
    assert.equal(await p.evaluate(()=>document.activeElement.id),'kpM1');
    await p.keyboard.press('Tab');
    assert.equal(await p.evaluate(()=>document.activeElement.id),'kpM2');
    assert(!s.calls.some(c=>c.body));
  });
  for (const model of [127,126]) await scenario(`keypad-busy-keeps-draft-controls-${model}`,async(p,s)=>{
    await keypad(p,s);
    const view=s.snapshot.controllers[0];
    view.product_id=model;s.snapshot.inventory.devices[2].product_id=model;
    view.editor_available=true;
    view.slots.forEach(row=>Object.assign(row,{draft_editable:row.editable,ha_draft_available:row.editable}));
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    await p.locator('#kpM2').selectOption('native');await p.locator('#kpG2').selectOption('301');
    for(let i=0;i<3;i++){
      view.actions.edit_assignment={enabled:false,reason:'operation_in_progress'};
      view.actions.read_assignments={enabled:false,reason:'operation_in_progress'};
      view.slots.forEach(row=>Object.assign(row,{editable:false,ha_available:false}));
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
      assert.equal(await p.locator('#kpM2').count(),1,'background work must not replace verified controls with summary text');
      assert.equal(await p.locator('.kp-mode').count(),3,'all eligible button rows stay visible while busy');
      assert.equal(await p.locator('#kpG2').inputValue(),'301');
      assert(await p.getByRole('button',{name:'Save 1 change',exact:true}).isDisabled());
      assert(await p.getByRole('button',{name:'Read button assignments',exact:true}).isDisabled());
      await p.evaluate(()=>saveKeypad(40003));
      await p.locator('#kpL2').selectOption(i%2?'128':'191');
      await p.locator('#kpM2').selectOption('ha');
      assert.equal(await p.locator('#kpM2').inputValue(),'ha');
      assert.equal(await p.locator('#kpM3').count(),0,'foreign protected assignment must stay read-only');
      view.actions.edit_assignment={enabled:true,reason:'available'};
      view.actions.read_assignments={enabled:true,reason:'available'};
      view.slots.forEach(row=>Object.assign(row,{editable:row.draft_editable,ha_available:row.ha_draft_available}));
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
      assert.equal(await p.locator('#kpM2').inputValue(),'ha');
      assert(await p.getByRole('button',{name:'Save 1 change',exact:true}).isEnabled());
      await p.locator('#kpM2').selectOption('native');
      assert.equal(await p.locator('#kpG2').inputValue(),'301');
    }
    assert(!s.calls.some(c=>c.body),'busy state and draft choices must never send a write');
    view.editor_available=false;view.assignment_read_back=false;view.revision=null;view.slots=[];
    view.actions.edit_assignment={enabled:false,reason:'read_assignments_first'};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert.equal(await p.locator('.kp-edit').count(),0,'lost readback must still revoke the editor');
  },true);
  await scenario('keypad-mouse-picker-survives-native-focus-loss',async(p,s)=>{
    await keypad(p,s);
    for(const id of ['kpM1','kpG1','kpL1']){
      const select=p.locator('#'+id);await select.click();
      const original=await select.elementHandle();
      // Native desktop menus can take focus out of the document without an
      // outside click. Simulate that focusout, not a committed selection.
      await select.evaluate(el=>el.blur());
      for(let age=1;age<=3;age++){
        s.snapshot.controllers[0].last_event={event_type:'on',button:1,age_ms:age*1000};
        await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
        assert(await original.evaluate(el=>el===document.getElementById(el.id)),'a native-menu focusout must not release and destroy its select');
      }
      await select.selectOption(id==='kpM1'?'native':id==='kpG1'?'301':'128');
      await p.waitForTimeout(180);
      assert(await p.evaluate(()=>activeNativeSelect===null));
    }
    assert.deepEqual(await p.evaluate(()=>keypadChanges(40003)),[{slot:1,mode:'native',group:301,brightness:128}]);
    await p.locator('#kpM1').click();await p.locator('#kpM1').evaluate(el=>el.blur());
    await p.locator('.drawer-head h3').click();await p.waitForTimeout(180);
    assert(await p.evaluate(()=>activeNativeSelect===null),'an explicit outside click releases a cancelled picker');
    await p.locator('#kpM1').click();await p.locator('#kpM1').evaluate(el=>el.blur());
    await p.keyboard.press('Escape');await p.waitForTimeout(180);
    assert(await p.evaluate(()=>activeNativeSelect===null&&S.drawer?.id===40003),'Escape releases a native picker after body focus without closing the drawer');
    assert(!s.calls.some(c=>c.body));
  });
  await scenario('settings-mouse-picker-keeps-connected-node',async(p,s)=>{
    s.handlers['/api/settings/mqtt']=({send})=>send({revision:'settings-one',enabled:true,host:'broker.example.invalid',port:1883,tls:false,connected:true,password_set:true});
    await p.evaluate(()=>go('settings'));await p.locator('[data-settings-panel=mqtt] summary').click();await p.locator('#mqttPasswordAction:not(:disabled)').waitFor();
    const select=p.locator('#mqttPasswordAction');await select.click();const original=await select.elementHandle();
    await select.evaluate(el=>el.blur());
    await p.evaluate(()=>{
      window.selectDetached=false;window.mainSelectRefocus=0;
      const selected=document.querySelector('#mqttPasswordAction');
      window.selectObserver=new MutationObserver(records=>{for(const record of records)for(const node of record.removedNodes)if(node===selected||node.contains?.(selected))window.selectDetached=true;});
      window.selectObserver.observe(document.querySelector('#main'),{childList:true,subtree:true});
      const focus=HTMLElement.prototype.focus;HTMLElement.prototype.focus=function(...args){if(this.id==='mqttPasswordAction')window.mainSelectRefocus++;return focus.apply(this,args);};
    });
    for(let age=1;age<=3;age++){
      s.snapshot.device_states={'40001':reported(age)};
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
      assert(await original.evaluate(el=>el===document.getElementById(el.id)));
      assert.equal(await p.evaluate(()=>window.selectDetached),false,'main form must not detach an open native picker');
    }
    await select.selectOption('replace');await p.waitForTimeout(180);
    assert.equal(await select.inputValue(),'replace');assert.equal(await p.evaluate(()=>window.mainSelectRefocus),0);
    assert(await p.locator('#mqttPassword').isEnabled());
    await select.click();await select.evaluate(el=>el.blur());
    await p.locator('#mqttHost').focus();await p.waitForTimeout(180);
    assert(await p.evaluate(()=>activeNativeSelect===null),'focus entering another control must release a cancelled picker');
    await select.click();await select.evaluate(el=>el.blur());
    await p.keyboard.press('Escape');await p.waitForTimeout(180);
    assert(await p.evaluate(()=>activeNativeSelect===null),'Escape must release a picker even when BODY has focus');
    assert(!s.calls.some(c=>c.body));
  });
  await scenario('smart-switch-power-works-alone-in-mixed-groups-and-rooms',async(p,s)=>{
    s.snapshot.inventory.devices[0].product_id=167;
    s.snapshot.inventory.groups[0].members=[40001,40002];
    s.snapshot.device_rooms={'40001':'Theatre','40002':'Theatre'};
    s.snapshot.device_states={'40001':reported(0),'40002':reported(0)};
    s.handlers['/api/control']=({body,send})=>{
      assert.deepEqual(Object.keys(body.values),['state']);
      return send({sent:true,feedback_pending:true,confirmed:false,readbacks:[]});
    };
    await p.evaluate(snapshot=>{applySnapshot(snapshot);openDrawer('device',40001);},clone(s.snapshot));
    assert.equal(await p.locator('#briR').count(),0);
    await p.evaluate(async()=>{await devicePower(40001,true);await devicePower(40001,false);openDrawer('group',300);});
    assert.equal(await p.locator('#gBri').count(),0);
    await p.evaluate(async()=>{await groupPower(300,true);await groupPower(300,false);go('home');});
    assert.equal(await p.getByLabel('Theatre brightness').count(),0);
    await p.evaluate(()=>roomPower('Theatre',true));
    s.snapshot.inventory.groups[0].members=[];
    await p.evaluate(async snapshot=>{applySnapshot(snapshot);await roomPower('Theatre',true);await roomPower('Theatre',false);},clone(s.snapshot));
    await p.evaluate(()=>{setBrightness(40001,50);roomBrightness('Theatre',50);});
    assert.deepEqual(s.calls.filter(c=>c.path==='/api/control').map(c=>[c.body.target,c.body.values.state]),
      [[40001,'ON'],[40001,'OFF'],[300,'ON'],[300,'OFF'],[300,'ON'],[40001,'ON'],[40002,'ON'],[40001,'OFF'],[40002,'OFF']]);
  });
  await scenario('power-totals-separate-current-and-awaiting-readings',async(p,s)=>{
    s.snapshot.inventory.groups[0].members=[40001,40002];
    s.snapshot.device_rooms={'40001':'Theatre','40002':'Theatre'};
    const show=()=>p.evaluate(snapshot=>{applySnapshot(snapshot);go('home');},clone(s.snapshot));
    await show();
    const counts=[p.locator('.view-head .sub'),p.locator('.room-head .count'),p.locator('.group-open .small')];
    for(const count of counts) assert((await count.innerText()).includes('Waiting for light status · 2 awaiting readings'));
    s.snapshot.device_states={'40001':reported(100),'40002':reported(0,3000,false)};await show();
    for(const count of counts) assert((await count.innerText()).includes('1 on · 0 off · 1 awaiting readings'));
    s.snapshot.device_states['40002']=reported(0);await show();
    for(const count of counts) assert((await count.innerText()).includes('1 on · 1 off'));
  },true);
  await scenario('manual-state-sweep-yields-and-stops-after-current-read',async(p,s)=>{
    s.features.read_states=true;
    s.snapshot.inventory.devices.push({device_id:40004,product_id:134,name:'Spare bulb'});
    await p.evaluate(async snapshot=>{S.caps=await api('/api/capabilities');applySnapshot(snapshot);go('home');},clone(s.snapshot));
    let releaseFirst, releaseSecond;
    const first=new Promise(r=>{releaseFirst=r;}), second=new Promise(r=>{releaseSecond=r;});
    s.handlers['/api/device/read']=async({body,send})=>{
      await (body.target===40001?first:second);
      return send({target:body.target,observed:{brightness:100}});
    };
    s.handlers['/api/control']=({send})=>send({sent:true,feedback_pending:true,confirmed:false,readbacks:[]});
    await p.getByRole('button',{name:'Read light states',exact:true}).click();
    await p.waitForFunction(()=>S.radio.step.includes('1 of 3'));
    await p.getByRole('button',{name:'Turn on Theatre 1',exact:true}).click();
    releaseFirst();
    await p.waitForFunction(()=>S.radio.step.includes('2 of 3'));
    await p.getByRole('button',{name:'Stop reading',exact:true}).click();releaseSecond();
    await p.waitForFunction(()=>!stateSweep&&!S.radio.label);
    assert.deepEqual(s.calls.filter(c=>['/api/device/read','/api/control'].includes(c.path)).map(c=>[c.path,c.body.target]),
      [['/api/device/read',40001],['/api/control',40001],['/api/device/read',40002]]);
    assert((await p.locator('#toastRoot').innerText()).includes('Read stopped'));
  },true);
  await scenario('flash-uses-live-level-and-confirms-unknown-switch',async(p,s)=>{
    s.snapshot.device_states={'40001':reported(255)};
    s.snapshot.inventory.devices[1].product_id=167;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    let level=48;
    s.handlers['/api/device/read']=({body,send})=>send({target:body.target,observed:level===null?{}:{brightness:level}});
    s.handlers['/api/control']=({send})=>send({sent:true,confirmed:true,readbacks:[]});
    const sent=()=>s.calls.filter(c=>c.path==='/api/control').map(c=>c.body.values);
    await p.evaluate(()=>flash(40001));
    assert.deepEqual(sent(),[{state:'OFF'},{state:'ON',brightness:48},{state:'OFF'},{state:'ON',brightness:48}]);
    level=null;
    await p.evaluate(()=>{window.flashPrompts=[];window.confirm=message=>{window.flashPrompts.push(message);return false;};return flash(40002);});
    assert.equal(sent().length,4);
    assert.match(await p.evaluate(()=>window.flashPrompts[0]),/leave it on\?/);
    await p.evaluate(()=>{window.confirm=()=>true;return flash(40002);});
    assert.deepEqual(sent().slice(4),[{state:'OFF'},{state:'ON'},{state:'OFF'},{state:'ON'}]);
    s.handlers['/api/control']=({send})=>send({sent:true,confirmed:false,readbacks:[]});
    level=96;await p.evaluate(()=>flash(40001));
    assert.equal(sent().length,9,'An unconfirmed pulse stops the flash');
  });
  await scenario('flash-yields-to-new-group-power-and-stops-after-reconnect',async(p,s)=>{
    s.snapshot.inventory.groups[0].members=[40001,40002];
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    s.handlers['/api/device/read']=({body,send})=>send({target:body.target,observed:{brightness:32}});
    let finishPulse;
    const pulse=new Promise(r=>{finishPulse=r;});
    s.handlers['/api/control']=async({body,send})=>{
      if(body.target===40001) await pulse;
      return send({sent:true,confirmed:true,readbacks:[{device_id:40001,brightness:0}]});
    };
    await p.evaluate(()=>{window.flashing=flash(40001);});
    await p.waitForFunction(()=>S.radio.step==='Blink 1 of 2');
    await p.evaluate(()=>{window.newPower=groupPower(300,false);});
    finishPulse();await p.evaluate(()=>Promise.all([window.flashing,window.newPower]));
    assert.deepEqual(s.calls.filter(x=>x.path==='/api/control').map(x=>x.body.target),[40001,300]);
    // A read returning after reconnect cannot authorize the old flash.
    let finishRead; const pending=new Promise(r=>{finishRead=r;});
    s.handlers['/api/device/read']=async({body,send})=>{await pending;return send({target:body.target,observed:{brightness:128}});};
    await p.evaluate(()=>{window.flashing=flash(40002);});
    await p.waitForFunction(()=>S.radio.step==='Reading the starting level');
    s.snapshot.status.instance='replacement';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    finishRead();await p.evaluate(()=>window.flashing);
    assert.equal(s.calls.filter(x=>x.path==='/api/control').length,2);
  });
  await scenario('group-switch-holds-intent-until-fresh-reports',async(p,s)=>{
    s.snapshot.device_states={'40001':reported(0),'40002':reported(0)};
    s.handlers['/api/control']=({send})=>send({sent:true,confirmed:true,readbacks:[{device_id:40001,brightness:255},{device_id:40002,brightness:255}]});
    await groupSnapshot(p,s);
    await p.getByLabel('Power',{exact:true}).check();
    await p.waitForFunction(()=>groupPowerIntents.size > 0);
    assert.equal(await p.getByText('Turning on…',{exact:true}).count(),0);
    await p.waitForFunction(()=>!S.radio.label);
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(await p.getByLabel('Power',{exact:true}).isChecked());
    assert(await p.getByLabel('Power',{exact:true}).isEnabled());
    assert(await p.evaluate(()=>S.devices.filter(d=>d.kind==='light').every(d=>d.brightness===0)),'Pending switch must not fabricate light state');
    const fresh=(n,at)=>Object.fromEntries(Object.entries(reported(n)).map(([k,v])=>[k,{...v,observed_ms:at}]));
    s.snapshot.device_states['40001']=fresh(255,2);
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(await p.getByLabel('Power',{exact:true}).isChecked());assert(await p.getByLabel('Power',{exact:true}).isEnabled());
    s.snapshot.device_states['40002']=fresh(255,2);
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.waitForFunction(()=>!groupPowerIntents.size);
    assert(await p.getByLabel('Power',{exact:true}).isChecked());
    await p.getByLabel('Power',{exact:true}).uncheck();
    await p.waitForFunction(()=>groupPowerIntents.size > 0);
    assert.equal(await p.getByText('Turning off…',{exact:true}).count(),0);
    assert(!await p.getByLabel('Power',{exact:true}).isChecked());
    await p.waitForFunction(()=>!S.radio.label);
    s.snapshot.device_states={'40001':fresh(0,3),'40002':fresh(0,3)};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.waitForFunction(()=>!groupPowerIntents.size);
    assert(!await p.getByLabel('Power',{exact:true}).isChecked());
    assert.equal(s.calls.filter(c=>c.path==='/api/control').length,2);
  });
  await scenario('group-switch-can-reverse-before-delayed-readbacks',async(p,s)=>{
    s.snapshot.device_states={'40001':reported(0),'40002':reported(0)};
    let release; const dispatched=new Promise(resolve=>release=resolve);
    s.handlers['/api/control']=async({body,send})=>{
      assert.equal(body.confirm,false);
      if(body.values.state==='ON')await dispatched;
      return send({sent:true,confirmed:false,readbacks:[],feedback_pending:true});
    };
    await groupSnapshot(p,s);
    await p.getByLabel('Power',{exact:true}).check();
    await p.waitForFunction(()=>!!S.radio.label);
    await p.waitForTimeout(180);
    assert(await p.getByLabel('Power',{exact:true}).isChecked());
    assert(await p.getByLabel('Power',{exact:true}).isEnabled());
    assert.equal(await p.locator('.busybar').count(),0);
    assert.equal(await p.locator('.group-power [role=status]').count(),0);
    await p.getByLabel('Power',{exact:true}).uncheck();
    await p.waitForTimeout(180);
    assert(!await p.getByLabel('Power',{exact:true}).isChecked());
    release();await p.waitForFunction(()=>!S.radio.label&&!S.radio.queue.length);
    assert.deepEqual(s.calls.filter(c=>c.path==='/api/control').map(c=>c.body.values.state),['ON','OFF']);
    assert.equal(await p.locator('#toastRoot .toast').count(),0,'Expected asynchronous reads need no warning');
    // Late ON readbacks cannot flip the newly requested OFF switch.
    const fresh=(n,at)=>Object.fromEntries(Object.entries(reported(n)).map(([k,v])=>[k,{...v,observed_ms:at}]));
    s.snapshot.device_states={'40001':fresh(255,2),'40002':fresh(255,2)};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(!await p.getByLabel('Power',{exact:true}).isChecked());
    assert(await p.evaluate(()=>S.devices.filter(d=>d.kind==='light').every(d=>d.brightness===255)));
    s.snapshot.device_states={'40001':fresh(0,3),'40002':fresh(0,3)};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.waitForFunction(()=>!groupPowerIntents.size);
    assert(!await p.getByLabel('Power',{exact:true}).isChecked());
  });
  await scenario('group-switch-three-reversals-ignore-earlier-command-reports',async(p,s)=>{
    s.snapshot.device_states={'40001':reported(0),'40002':reported(0)};
    let release, command=0; const firstPending=new Promise(resolve=>release=resolve);
    s.handlers['/api/control']=async({body,send})=>{
      assert.equal(body.confirm,false);
      const boundary=++command*10;
      if(boundary===10)await firstPending;
      return send({sent:true,confirmed:false,readbacks:[],feedback_pending:true,feedback_after_ms:boundary});
    };
    await groupSnapshot(p,s);
    await p.getByLabel('Power',{exact:true}).check();
    await p.waitForFunction(()=>!!S.radio.label);
    await p.waitForTimeout(180);
    await p.getByLabel('Power',{exact:true}).uncheck();
    await p.waitForTimeout(180);
    await p.getByLabel('Power',{exact:true}).check();
    await p.waitForTimeout(180);
    assert(await p.getByLabel('Power',{exact:true}).isEnabled());
    release();await p.waitForFunction(()=>!S.radio.label&&!S.radio.queue.length);
    assert.deepEqual(s.calls.filter(c=>c.path==='/api/control').map(c=>c.body.values.state),['ON','OFF','ON']);
    const fresh=(n,at)=>Object.fromEntries(Object.entries(reported(n)).map(([k,v])=>[k,{...v,observed_ms:at}]));
    // First ON reports must not settle the final ON: its command boundary is 30.
    s.snapshot.device_states={'40001':fresh(255,11),'40002':fresh(255,11)};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(await p.evaluate(()=>groupPowerIntents.size===1),'First ON reports cannot settle the final ON intent');
    assert(await p.getByLabel('Power',{exact:true}).isChecked());
    // The middle OFF then arrives, without changing the requested switch position.
    s.snapshot.device_states={'40001':fresh(0,21),'40002':fresh(0,21)};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
    assert(await p.getByLabel('Power',{exact:true}).isChecked());
    assert(await p.evaluate(()=>S.devices.filter(d=>d.kind==='light').every(d=>d.brightness===0)),
      'Desired switch position must not replace reported light readings');
    s.snapshot.device_states={'40001':fresh(255,31),'40002':fresh(255,31)};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.waitForFunction(()=>!groupPowerIntents.size);
    assert(await p.getByLabel('Power',{exact:true}).isChecked());
    assert.equal(await p.locator('#toastRoot .toast').count(),0);
    assert.equal(await p.locator('.busybar').count(),0);
  });
  await scenario('group-switch-rejection-and-timeout-restore-reports',async(p,s)=>{
    s.snapshot.device_states={'40001':reported(0),'40002':reported(0)};
    s.handlers['/api/control']=({send})=>send({error:'mesh_busy',operation_started:false,requires_review:false,retry_allowed:false},409);
    await groupSnapshot(p,s);await p.getByLabel('Power',{exact:true}).check();
    await p.waitForFunction(()=>!S.radio.label&&!groupPowerIntents.size);
    await p.waitForTimeout(180);
    assert(!await p.getByLabel('Power',{exact:true}).isChecked());assert(await p.getByLabel('Power',{exact:true}).isEnabled());
    assert.equal(s.calls.filter(c=>c.path==='/api/control').length,1);
    s.handlers['/api/control']=({send})=>send({sent:true,confirmed:false,readbacks:[]});
    await p.getByLabel('Power',{exact:true}).check();
    await p.waitForFunction(()=>groupPowerIntents.size > 0);
    assert.equal(await p.getByText('Turning on…',{exact:true}).count(),0);
    await p.waitForFunction(()=>!groupPowerIntents.size,undefined,{timeout:10000});
    await p.waitForTimeout(180);
    assert(!await p.getByLabel('Power',{exact:true}).isChecked());
    assert.equal(s.calls.filter(c=>c.path==='/api/control').length,2,'Unconfirmed control must not be replayed');
  });

  await scenario('group-sliders-follow-reports',async(p,s)=>{
    s.snapshot.device_states={'40001':reported(51),'40002':reported(51)};
    await groupSnapshot(p,s);
    await p.waitForFunction(()=>document.querySelector('#gBri')?.value==='20');
    assert.equal(await p.locator('#gBriV').textContent(),'20%');
    assert.equal(await p.locator('#ctR').inputValue(),'3000');
    s.snapshot.device_states={'40001':reported(204,4000),'40002':reported(204,4000)};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.waitForFunction(()=>document.querySelector('#gBri')?.value==='80');
    assert.equal(await p.locator('#gBriV').textContent(),'80%');
    assert.equal(await p.locator('#ctR').inputValue(),'4000');
    await p.screenshot({path:path.join(evidence,'group-reported-sliders.png'),fullPage:true});
    assert(!s.calls.some(c=>c.path==='/api/control'));
  });
  await scenario('group-mixed-partial-unknown-and-off',async(p,s)=>{
    s.snapshot.device_states={'40001':reported(51,3000),'40002':reported(204,4000)};
    await groupSnapshot(p,s);
    await p.waitForFunction(()=>document.querySelector('#gBri')?.value==='80');
    assert(await p.getByText('Lights report different levels; the slider shows the highest.').isVisible());
    assert.equal(await p.locator('#ctV').textContent(),'–');
    delete s.snapshot.device_states['40002'];
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.waitForFunction(()=>document.querySelector('#gBri')?.value==='20');
    assert(await p.getByText('Based on readings from 1 of 2 lights.').isVisible());
    s.snapshot.device_states={'40001':reported(51,3000,false)};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.waitForFunction(()=>document.querySelector('#gBriV')?.textContent==='–');
    assert.equal(await p.locator('#gBri').inputValue(),'0');
    s.snapshot.device_states={'40001':reported(0),'40002':reported(0)};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.waitForFunction(()=>document.querySelector('#gBriV')?.textContent==='0%');
    assert(!s.calls.some(c=>c.path==='/api/control'));
  });
  await scenario('group-slider-command-awaits-observation',async(p,s)=>{
    s.snapshot.device_states={'40001':reported(51),'40002':reported(51)};
    s.handlers['/api/control']=({send})=>send({sent:true,readbacks:[],confirmed:false});
    await groupSnapshot(p,s);
    await p.waitForFunction(()=>document.querySelector('#gBri')?.value==='20');
    await p.locator('#gBri').fill('35');
    await p.waitForFunction(()=>!S.radio.label && !S.pending[300]);
    assert(await p.getByText('Sent, but no light replied. The change is not confirmed.',{exact:true}).isVisible());
    await p.waitForFunction(()=>document.querySelector('#gBri')?.value==='20');
    const calls=s.calls.filter(c=>c.path==='/api/control');assert.equal(calls.length,1);
    assert.deepEqual(calls[0].body,{target:300,values:{state:'ON',brightness:89},confirm:false});
    s.snapshot.device_states={'40001':reported(89),'40002':reported(89)};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.waitForFunction(()=>document.querySelector('#gBri')?.value==='35');
    assert.equal(await p.locator('#gBriV').textContent(),'35%');
  });
  await scenario('group-control-partial-feedback',async(p,s)=>{
    s.snapshot.device_states={'40001':reported(51),'40002':reported(51)};
    s.handlers['/api/control']=({send})=>send({sent:true,readbacks:[{device_id:40001,brightness:89}],confirmed:false});
    await groupSnapshot(p,s);
    await p.locator('#gBri').fill('35');
    await p.waitForFunction(()=>!S.radio.label && !S.pending[300]);
    assert(await p.getByText('Sent; 1 light replied, but the full change is not confirmed.',{exact:true}).isVisible());
    await p.waitForFunction(()=>document.querySelector('#gBri')?.value==='20');
    assert.equal(s.calls.filter(c=>c.path==='/api/control').length,1);
    assert(!await p.evaluate(()=>S.log.some(entry=>entry.detail.includes('[object Object]'))));
    await p.screenshot({path:path.join(evidence,'group-control-partial-feedback.png'),fullPage:true});
  });
  for(const [name,body] of [
    ['control-busy-refusal-no-retry',{error:'mesh_busy',operation_started:false,requires_review:false,retry_allowed:false,message:'Busy; no control was started.'}],
    ['control-uncertain-response-no-retry',{error:'operation_unavailable_or_uncertain',message:'Control outcome needs review.'}],
  ]) await scenario(name,async(p,s)=>{
    s.snapshot.device_states={'40001':reported(51),'40002':reported(51)};
    s.handlers['/api/control']=({send})=>send(body,409);
    await groupSnapshot(p,s);
    await p.locator('#gBri').fill('35');
    await p.getByText(body.message,{exact:true}).waitFor();
    await p.waitForFunction(()=>!S.radio.label && !S.pending[300]);
    assert.equal(s.calls.filter(c=>c.path==='/api/control').length,1);
    await p.waitForFunction(()=>document.querySelector('#gBri')?.value==='20');
  });
  await scenario('4000k-preset-device-and-group',async(p,s)=>{
    s.snapshot.device_states={'40001':reported(51,4000),'40002':reported(51,4000)};
    s.handlers['/api/control']=({send})=>send({sent:true,readbacks:[],confirmed:false});
    await groupSnapshot(p,s);
    const preset=p.getByRole('button',{name:'Cool white 4000K'});
    assert.equal(await preset.getAttribute('aria-pressed'),'true');
    await preset.click();await p.waitForFunction(()=>!S.radio.label && !S.pending[300]);
    await p.evaluate(()=>openDrawer('device',40001));
    assert.equal(await preset.getAttribute('aria-pressed'),'true');
    await preset.click();await p.waitForFunction(()=>!S.radio.label && !S.pending[40001]);
    assert.deepEqual(s.calls.filter(c=>c.path==='/api/control').map(c=>c.body),[
      {target:300,values:{color_temp:4000},confirm:false},{target:40001,values:{color_temp:4000},confirm:false}]);
  });
  async function reviewMembershipReceipt(p, s, id) {
    s.handlers['/api/transactions']=({send})=>send({transactions:[
      {id,target:40001,kind:'group_membership',status:'uncertain',detail:{},recovery:{actions:[]}}]});
    await p.getByRole('button',{name:'Review this operation',exact:true}).click();
    const card=p.locator('.ops-card'); await card.waitFor();
    await card.getByText('Technical record',{exact:true}).click();
    assert.equal(JSON.parse(await card.locator('pre').innerText()).id,id);
  }
  const seedRoomGroup = async(p,s) => {
    s.snapshot.inventory.devices[0].product_id=134;s.snapshot.inventory.devices[1].product_id=134;
    s.snapshot.device_rooms={'40001':'Living Room','40002':'Living Room','40003':'Living Room'};
    await p.evaluate(snapshot=>{applySnapshot(snapshot);go('home');},clone(s.snapshot));
  };
  for(const mobile of [false,true]) await scenario('group-from-room-'+(mobile?'phone':'desktop'),async(p,s)=>{
    await seedRoomGroup(p,s);
    await p.getByRole('button',{name:'Create group from room',exact:true}).click();
    assert.equal(await p.locator('#newGroupName').inputValue(),'Living Room');
    assert(!s.calls.some(c=>c.body));
    await p.locator('#newGroupName').fill('Living Room Lamps');
    await p.locator('#createGroupSubmit').click();
    await p.getByRole('heading',{name:'Add lights to Living Room Lamps'}).waitFor();
    assert(await p.getByLabel('Theatre 1',{exact:true}).isChecked());
    assert(await p.getByLabel('Theatre 2',{exact:true}).isChecked());
    assert.equal(await p.locator('.modal input[type="checkbox"]').count(),2,'controllers excluded');
    assert(!s.calls.some(c=>c.path==='/api/rooms/membership'));
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await p.screenshot({path:path.join(evidence,'room-group-review-'+(mobile?'phone':'desktop')+'.png'),fullPage:true});
    await p.locator('#applyGroupMembers').click();
    await p.getByText('Selected lights have verified physical membership.').waitFor();
    assert.deepEqual(s.calls.find(c=>c.path==='/api/rooms/membership').body,{targets:[40001,40002],group:301,member:true});
    assert.equal(s.calls.filter(c=>c.path==='/api/metadata').length,1);
    assert.equal(s.calls.filter(c=>c.path==='/api/rooms/membership').length,1);
    assert.deepEqual(s.snapshot.device_rooms,{'40001':'Living Room','40002':'Living Room','40003':'Living Room'});
    await p.evaluate(()=>{closeDrawer();go('home');});
    assert(await p.getByText('Room controls use the Living Room Lamps group.',{exact:true}).isVisible());
    assert.equal(await p.getByRole('button',{name:'Create group from room',exact:true}).count(),0);
    await p.getByRole('button',{name:'Open group',exact:true}).click();
    assert.equal(s.calls.filter(c=>c.path==='/api/metadata').length,1);
  },mobile);
  await scenario('group-from-room-reuses-cancelled-empty-group',async(p,s)=>{
    await seedRoomGroup(p,s);await p.evaluate(()=>openCreateGroup('Living Room'));
    await p.locator('#createGroupSubmit').click();await p.locator('#applyGroupMembers').waitFor();
    await p.getByRole('button',{name:'Cancel',exact:true}).click();
    await p.evaluate(()=>{closeDrawer();go('home');openCreateGroup('Living Room');});
    await p.getByRole('heading',{name:'Add lights to Living Room'}).waitFor();
    assert(await p.getByLabel('Theatre 1',{exact:true}).isChecked());
    assert(await p.getByLabel('Theatre 2',{exact:true}).isChecked());
    assert.equal(s.calls.filter(c=>c.path==='/api/metadata').length,1,'reuse the empty group after cancelling its member review');
    assert(!s.calls.some(c=>c.path==='/api/rooms/membership'));
  });
  await scenario('group-from-room-review-changes-cancel-and-ineligible',async(p,s)=>{
    await seedRoomGroup(p,s);await p.evaluate(()=>openCreateGroup('Living Room'));
    await p.getByRole('button',{name:'Cancel',exact:true}).click();assert(!s.calls.some(c=>c.body));
    s.snapshot.group_workflow.lights[1].eligible=false;
    await p.evaluate(snapshot=>{applySnapshot(snapshot);openCreateGroup('Living Room');},clone(s.snapshot));
    await p.getByText(/Not included because group setup is not supported/).waitFor();
    await p.locator('#createGroupSubmit').click();await p.locator('#applyGroupMembers').waitFor();
    assert(await p.getByLabel('Theatre 1',{exact:true}).isChecked());
    assert.equal(await p.getByLabel('Theatre 2',{exact:true}).count(),0);
    await p.getByLabel('Theatre 1',{exact:true}).uncheck();assert(await p.locator('#applyGroupMembers').isDisabled());
    await p.getByLabel('Theatre 1',{exact:true}).check();
    s.snapshot.device_rooms['40001']='Elsewhere';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.locator('#applyGroupMembers').click();assert(!s.calls.some(c=>c.path==='/api/rooms/membership'));
  });
  await scenario('group-from-room-stale-and-zero-eligible-send-nothing',async(p,s)=>{
    await seedRoomGroup(p,s);await p.evaluate(()=>openCreateGroup('Living Room'));
    s.snapshot.device_rooms['40002']='Elsewhere';await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.locator('#createGroupSubmit').click();assert(!s.calls.some(c=>c.body));
    await p.evaluate(()=>closeModal());s.snapshot.group_workflow.lights.forEach(d=>d.eligible=false);
    await p.evaluate(snapshot=>{applySnapshot(snapshot);openCreateGroup('Living Room');},clone(s.snapshot));
    assert(await p.locator('#createGroupSubmit').isDisabled());await p.locator('#newGroupName').press('Enter');
    assert(!s.calls.some(c=>c.body));
  });
  await scenario('group-from-room-refresh-failure-no-repeat-or-programming',async(p,s)=>{
    await seedRoomGroup(p,s);await p.evaluate(()=>openCreateGroup('Living Room'));s.snapshotFail=true;
    await p.locator('#createGroupSubmit').click();await p.getByText(/Group created, but the view could not refresh/).waitFor();
    await p.locator('#newGroupName').press('Enter');await p.evaluate(()=>createNamedGroup());
    assert.equal(s.calls.filter(c=>c.path==='/api/metadata').length,1);assert(!s.calls.some(c=>c.path==='/api/rooms/membership'));
  });
  await scenario('group-from-room-batch-limit-and-partial-results',async(p,s)=>{
    await seedRoomGroup(p,s);
    for(let n=4;n<=34;n++){
      s.snapshot.inventory.devices.push({device_id:40000+n,product_id:134,name:'Lamp '+n});
      s.snapshot.device_rooms[40000+n]='Living Room';
      s.snapshot.group_workflow.lights.push({target:40000+n,name:'Lamp '+n,eligible:true});
    }
    await p.evaluate(snapshot=>{applySnapshot(snapshot);openCreateGroup('Living Room');},clone(s.snapshot));
    await p.locator('#createGroupSubmit').click();await p.locator('#applyGroupMembers').waitFor();
    assert(await p.locator('#applyGroupMembers').isDisabled(),'33 lights must not exceed API batch limit');
    await p.getByLabel('Lamp 34',{exact:true}).uncheck();assert(await p.locator('#applyGroupMembers').isEnabled());
    s.handlers['/api/rooms/membership']=({send})=>send({physical_memberships_verified:false,message:'Stopped for review.',rows:[{target:40001,status:'needs_readback',transaction:'receipt-room'}]});
    await p.locator('#applyGroupMembers').click();await p.getByText(/Needs readback/).waitFor();
    assert(await p.getByRole('button',{name:'Add lights',exact:true}).isDisabled());
    await reviewMembershipReceipt(p,s,'receipt-room');
    assert.equal(s.calls.filter(c=>c.path==='/api/rooms/membership').length,1);
  });
  await scenario('create-empty-group',async(p,s)=>{
    await p.getByRole('button',{name:'Groups',exact:true}).click();await p.getByRole('button',{name:'Create group',exact:true}).click();
    await p.locator('#newGroupName').fill('Theatre spots');await p.locator('#createGroupSubmit').click();await p.getByRole('heading',{name:'Theatre spots'}).waitFor();
    assert.equal(s.calls.filter(c=>c.path==='/api/metadata').length,1);assert(!s.calls.some(c=>c.path==='/api/rooms/membership'));
  });
  await scenario('create-refresh-failure',async(p,s)=>{
    await p.evaluate(()=>openCreateGroup());await p.locator('#newGroupName').fill('New group');s.snapshotFail=true;
    await p.locator('#createGroupSubmit').click();await p.getByText(/Group created, but the view could not refresh/).waitFor();
    assert(await p.locator('#createGroupSubmit').isDisabled());assert.equal(s.calls.filter(c=>c.path==='/api/metadata').length,1);
  });
  await scenario('add-explicit-selection',async(p,s)=>{
    await group(p);await p.getByRole('button',{name:'Add lights',exact:true}).click();assert(await p.locator('#applyGroupMembers').isDisabled());
    await p.getByLabel('Theatre 1',{exact:true}).check();await p.locator('#applyGroupMembers').click();await p.getByText('Selected lights have verified physical membership.').waitFor();
    assert.deepEqual(s.calls.find(c=>c.path==='/api/rooms/membership').body,{targets:[40001],group:300,member:true});
    await p.screenshot({path:path.join(evidence,'group-result.png'),fullPage:true});
  });
  await scenario('gates-protected-and-stale',async(p,s)=>{
    await group(p);await p.evaluate(()=>{S.groupWorkflow.programming_enabled=false;render();});assert(await p.getByRole('button',{name:'Add lights',exact:true}).isDisabled());
    await p.evaluate(()=>{S.groupWorkflow.programming_enabled=true;S.groups[0].protected=true;render();});assert.equal(await p.getByRole('button',{name:'Add lights',exact:true}).count(),0);
    await p.evaluate(()=>{S.groups[0].protected=false;snapshotAt=0;openGroupMembers(300,true);});assert.equal(await p.locator('#applyGroupMembers').count(),0);
    assert(!s.calls.some(c=>c.path==='/api/rooms/membership'));
  });
  await scenario('confirmation-rechecks-revision',async(p,s)=>{
    await group(p);await p.getByRole('button',{name:'Add lights',exact:true}).click();await p.getByLabel('Theatre 1',{exact:true}).check();
    await p.evaluate(()=>{window.confirm=()=>{S.revision='changed';return true;};});await p.locator('#applyGroupMembers').click();
    assert(!s.calls.some(c=>c.path==='/api/rooms/membership'));
  });
  await scenario('partial-results-no-retry',async(p,s)=>{
    s.handlers['/api/rooms/membership']=({send})=>send({physical_memberships_verified:false,message:'Stopped; nothing resumes automatically.',rows:[{target:40001,status:'needs_readback',transaction:'receipt-1'},{target:40002,status:'not_attempted'}]});
    await group(p);await p.getByRole('button',{name:'Add lights',exact:true}).click();await p.getByLabel('Theatre 1',{exact:true}).check();await p.getByLabel('Theatre 2',{exact:true}).check();
    await p.locator('#applyGroupMembers').click();await p.getByText(/Needs readback/).waitFor();assert(await p.getByRole('button',{name:'Add lights',exact:true}).isDisabled());
    assert(await p.getByText(/Not attempted/).isVisible());
    await reviewMembershipReceipt(p,s,'receipt-1');
    assert.equal(s.calls.filter(c=>c.path==='/api/rooms/membership').length,1);
  });
  await scenario('lost-membership-response-no-retry',async(p,s)=>{
    s.handlers['/api/rooms/membership']=({route})=>route.abort();await group(p);await p.getByRole('button',{name:'Add lights',exact:true}).click();await p.getByLabel('Theatre 1',{exact:true}).check();await p.locator('#applyGroupMembers').click();
    await p.getByText(/Membership was not confirmed/).waitFor();assert(await p.getByRole('button',{name:'Add lights',exact:true}).isDisabled());assert.equal(s.calls.filter(c=>c.path==='/api/rooms/membership').length,1);
  });
  await scenario('background-contention-not-started-can-be-reviewed-again',async(p,s)=>{
    s.handlers['/api/rooms/membership']=({send})=>send({error:'background_read_busy',message:'The bridge is finishing a state read. No requested operation was started.',operation_started:false,requires_review:false,retry_allowed:false},409);
    await group(p);await p.getByRole('button',{name:'Add lights',exact:true}).click();await p.getByLabel('Theatre 1',{exact:true}).check();await p.locator('#applyGroupMembers').click();
    await p.getByText(/Group change not started/).waitFor();assert(await p.getByRole('button',{name:'Add lights',exact:true}).isEnabled());
    assert.equal(await p.getByRole('button',{name:'Review Operations',exact:true}).count(),0);assert.equal(s.calls.filter(c=>c.path==='/api/rooms/membership').length,1);
    await p.screenshot({path:path.join(evidence,'membership-not-started.png'),fullPage:true});
  });
  await scenario('pending-operation-stays-blocked-with-no-new-membership',async(p,s)=>{
    s.handlers['/api/rooms/membership']=({send})=>send({error:'pending_operation',message:'An unfinished operation needs review. This request was not started.',operation_started:false,requires_review:true,retry_allowed:false},409);
    await group(p);await p.getByRole('button',{name:'Add lights',exact:true}).click();await p.getByLabel('Theatre 1',{exact:true}).check();await p.locator('#applyGroupMembers').click();
    await p.getByText(/Group change not started/).waitFor();assert(await p.getByRole('button',{name:'Add lights',exact:true}).isDisabled());
    assert(await p.getByRole('button',{name:'Review Operations',exact:true}).isVisible());assert.equal(s.calls.filter(c=>c.path==='/api/rooms/membership').length,1);
  });
  await scenario('controller-status-and-correlated-read',async(p,s)=>{
    await p.evaluate(()=>openDrawer('device',40003));await p.getByText('MQTT is not enabled. Set it up in Settings to send events to Home Assistant.').waitFor();
    await p.getByRole('button',{name:'Read button assignments'}).click();await p.getByText('Read accepted. Waiting for both table reads.').waitFor();
    await p.evaluate(()=>{S.status.jobs[40003]={id:'old',state:'completed'};render();});assert(await p.getByRole('button',{name:'Read button assignments'}).isDisabled());
    await p.evaluate(()=>{S.status.jobs[40003]={id:'read-one',state:'completed'};S.controllerViews[0].assignment_read_back=true;render();});assert(await p.getByRole('button',{name:'Read button assignments'}).isEnabled());
    assert.equal(s.calls.filter(c=>c.path==='/api/keypad/read').length,1);assert.equal(await p.getByRole('link',{name:'Configure controller'}).count(),0);
  });
  await scenario('keypad-reviewed-multi-button-save',async(p,s)=>{
    s.handlers['/api/keypad/save']=({send})=>{acceptKeypad(s,[{slot:1,state:'writing'},{slot:2,state:'waiting'}]);return send({accepted:true,job_id:'save-one'},202);};
    await keypad(p,s);
    await p.locator('#kpM1').selectOption('unassigned');
    await p.locator('#kpM2').selectOption('native');
    await p.locator('#kpG2').selectOption('301');
    await p.getByLabel('Button 2 brightness',{exact:true}).selectOption('128');
    await p.getByRole('button',{name:'Save 2 changes',exact:true}).click();
    await p.getByText('Save accepted. Writing and checking each button.',{exact:true}).waitFor();
    const calls=s.calls.filter(c=>c.path==='/api/keypad/save');
    assert.deepEqual(calls.map(c=>c.body),[{target:40003,revision:'keypad-rev-1',changes:[
      {slot:1,mode:'unassigned'},{slot:2,mode:'native',group:301,brightness:128}]}]);
    assert.equal(s.dialogs.length,1);
    assert(s.dialogs[0].includes('Button 1: Theatre at 78% → Unused'));
    assert(s.dialogs[0].includes('Button 2: Unused → Bar at 50%'));
    await p.evaluate(()=>saveKeypad(40003));
    assert.equal(s.calls.filter(c=>c.path==='/api/keypad/save').length,1);
    await p.screenshot({path:path.join(evidence,'keypad-reviewed-save.png'),fullPage:true});
  });
  await scenario('keypad-protected-button-read-only',async(p,s)=>{
    await keypad(p,s);
    assert.equal(await p.locator('#kpG3').count(),0);
    assert.equal(await p.getByLabel('Button 3 brightness',{exact:true}).count(),0);
    assert(await p.getByText('Protected or unknown assignment · HA keypad target (cannot be changed here)',{exact:true}).isVisible());
    assert.equal(await p.locator('.kp-edit option[value="24575"]').count(),0);
    assert(await p.locator('#kpG1').isEnabled());
    assert(await p.getByRole('button',{name:'Save changes',exact:true}).isDisabled());
    assert(!s.calls.some(c=>c.path==='/api/keypad/save'));
    await p.screenshot({path:path.join(evidence,'keypad-protected-button.png'),fullPage:true});
  });
  await scenario('keypad-duplicate-group-blocks-save',async(p,s)=>{
    await keypad(p,s);
    await p.locator('#kpM2').selectOption('native');
    await p.locator('#kpG2').selectOption('300');
    await p.getByText('Theatre is chosen for more than one button. Each button needs its own group.',{exact:true}).waitFor();
    assert(await p.getByRole('button',{name:'Save 1 change',exact:true}).isDisabled());
    await p.evaluate(()=>saveKeypad(40003));
    assert(!s.calls.some(c=>c.path==='/api/keypad/save'));
    assert.equal(s.dialogs.length,0);
  });
  await scenario('keypad-progress-with-invalidated-binding',async(p,s)=>{
    s.handlers['/api/keypad/save']=({send})=>{acceptKeypad(s);return send({accepted:true,job_id:'save-one'},202);};
    await keypad(p,s);
    await p.locator('#kpM2').selectOption('native');
    await p.locator('#kpG2').selectOption('301');
    await p.getByRole('button',{name:'Save 1 change',exact:true}).click();
    await p.getByText('Button 2: Writing and checking…',{exact:true}).waitFor();
    assert(await p.getByText('Stored assignments have not been read back.',{exact:true}).isVisible());
    assert.equal(await p.locator('.kp-edit').count(),0);
    assert(await p.getByRole('button',{name:'Read button assignments',exact:true}).isDisabled());
    assert.equal(s.calls.filter(c=>c.path==='/api/keypad/save').length,1);
    await p.screenshot({path:path.join(evidence,'keypad-progress-without-binding.png'),fullPage:true});
  });
  await scenario('keypad-uncertain-save-review-without-retry',async(p,s)=>{
    s.handlers['/api/keypad/save']=({send})=>{
      acceptKeypad(s,[{slot:2,state:'needs_review'}]);
      s.snapshot.status.jobs[40003].state='needs_review';
      s.snapshot.status.keypad_saves[40003].state='needs_review';
      s.snapshot.status.pending_transactions=1;blockKeypad(s,'resolve_pending_operations');
      return send({accepted:true,job_id:'save-one'},202);
    };
    await keypad(p,s);
    await p.locator('#kpM2').selectOption('native');
    await p.locator('#kpG2').selectOption('301');
    await p.getByRole('button',{name:'Save 1 change',exact:true}).click();
    await p.getByText('A button change could not be confirmed. Nothing is retried automatically.',{exact:true}).waitFor();
    const review=p.getByRole('button',{name:'Review Operations',exact:true});
    assert(await review.isVisible());assert.equal(await p.locator('a[href*="classic"]').count(),0);
    assert(await p.getByText('Button 2: Needs review',{exact:true}).isVisible());
    await p.evaluate(async()=>{await refreshSnapshot();await refreshSnapshot();await saveKeypad(40003);});
    assert.equal(s.calls.filter(c=>c.path==='/api/keypad/save').length,1);
    assert(!s.calls.some(c=>c.path==='/api/keypad/read'));
    assert.equal(await p.locator('.kp-edit').count(),0);
    await p.screenshot({path:path.join(evidence,'keypad-uncertain-review.png'),fullPage:true});
  });
  await scenario('keypad-completed-save-shows-verified-table',async(p,s)=>{
    s.handlers['/api/keypad/save']=({send})=>{
      s.snapshot.controllers[0].revision='keypad-rev-2';
      Object.assign(s.snapshot.controllers[0].slots[1],{target:301,preset:128,status:'verified_mapping'});
      s.snapshot.status.jobs[40003]={id:'save-one',state:'completed',result:{verified:true,changed:true,buttons:[2]}};
      s.snapshot.status.keypad_saves[40003]={state:'verified',buttons:[{slot:2,state:'verified'}]};
      return send({accepted:true,job_id:'save-one'},202);
    };
    await keypad(p,s);
    await p.locator('#kpM2').selectOption('native');
    await p.locator('#kpG2').selectOption('301');
    await p.getByLabel('Button 2 brightness',{exact:true}).selectOption('128');
    await p.getByRole('button',{name:'Save 1 change',exact:true}).click();
    await p.getByText('All changes saved and verified by reading the keypad back.',{exact:true}).waitFor();
    assert(await p.getByText('Button 2: Saved and verified',{exact:true}).isVisible());
    assert.equal(await p.locator('#kpG2').inputValue(),'301');
    assert.equal(await p.getByLabel('Button 2 brightness',{exact:true}).inputValue(),'128');
    assert(await p.getByRole('button',{name:'Save changes',exact:true}).isDisabled());
    assert.equal(await p.getByRole('button',{name:'Review Operations',exact:true}).count(),0);
    await p.evaluate(()=>refreshSnapshot());
    assert.equal(s.calls.filter(c=>c.path==='/api/keypad/save').length,1);
    await p.screenshot({path:path.join(evidence,'keypad-completed-save.png'),fullPage:true});
  });
  await scenario('keypad-ha-save-keeps-other-buttons-and-reloads',async(p,s)=>{
    s.handlers['/api/keypad/save']=({body,send})=>{
      assert.deepEqual(body,{target:40003,revision:'keypad-rev-1',changes:[{slot:1,mode:'ha'}]});
      const v=s.snapshot.controllers[0];v.revision='keypad-rev-2';v.ha_status='publishing_enabled';
      Object.assign(v.slots[0],{target:24574,preset:255,assignment_mode:'ha',ha_owned:true,ha_ready:true});
      s.snapshot.status.jobs[40003]={id:'save-one',state:'completed',result:{verified:true,changed:true,buttons:[1]}};
      s.snapshot.status.keypad_saves[40003]={state:'verified',buttons:[{slot:1,state:'verified'}]};
      return send({accepted:true,job_id:'save-one'},202);
    };
    await keypad(p,s);
    const preserved=clone(s.snapshot.controllers[0].slots.slice(1));
    assert.equal(await p.locator('.attention').getByText('"HA keypad target" has no lights',{exact:true}).count(),0);
    assert.equal(await p.locator('.attention').getByText('"Theatre" has no lights',{exact:true}).count(),1);
    await p.getByLabel('Button 1 control',{exact:true}).selectOption('ha');
    await p.locator('#kpG1').waitFor({state:'detached'});
    assert.equal(await p.locator('#kpG1').count(),0);
    assert.equal(await p.getByLabel('Button 1 brightness',{exact:true}).count(),0);
    await p.getByRole('button',{name:'Save 1 change',exact:true}).click();
    await p.getByText('All changes saved and verified by reading the keypad back.',{exact:true}).waitFor();
    assert(s.dialogs[0].includes('Button 1: Theatre at 78% → Home Assistant (automation required)'));
    assert.deepEqual(s.snapshot.controllers[0].slots.slice(1),preserved);
    assert.equal(s.calls.filter(c=>c.path==='/api/keypad/save').length,1);
    assert(!s.calls.some(c=>c.path==='/api/ha/exposure'||c.path==='/api/keypad/ha/setup'));
    await p.reload();await p.waitForFunction(()=>S.synced);
    await p.evaluate(()=>openDrawer('device',40003));
    assert.equal(await p.getByLabel('Button 1 control',{exact:true}).inputValue(),'ha');
    assert(await p.getByRole('button',{name:'Save changes',exact:true}).isDisabled());
    assert(await p.getByText(/actual Home Assistant receipt is not verified here/).isVisible());
    await p.screenshot({path:path.join(evidence,'keypad-ha-mode-persisted.png'),fullPage:true});
  });
  await scenario('keypad-ha-mobile-and-direct-return',async(p,s)=>{
    await keypad(p,s);
    const v=s.snapshot.controllers[0];v.ha_status='not_exposed';
    Object.assign(v.slots[0],{target:24574,preset:255,assignment_mode:'ha',ha_owned:true,ha_ready:false});
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/Choose Add to Home Assistant below to publish its button events/).waitFor();
    assert.equal(await p.getByLabel('Button 1 control',{exact:true}).inputValue(),'ha');
    assert.equal(await p.locator('#kpM3').count(),0,'unowned protected slot stays readonly');
    await p.screenshot({path:path.join(evidence,'keypad-ha-mobile.png'),fullPage:true});
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    const drawerWidth=await p.locator('.drawer').evaluate(e=>({scroll:e.scrollWidth,client:e.clientWidth}));
    assert(drawerWidth.scroll<=drawerWidth.client,'no horizontal overflow inside the mobile drawer');
    await p.getByLabel('Button 1 control',{exact:true}).selectOption('native');
    assert(await p.getByRole('button',{name:'Save changes',exact:true}).isDisabled());
    await p.getByLabel('Button 1 group',{exact:true}).selectOption('300');
    assert(await p.getByRole('button',{name:'Save 1 change',exact:true}).isEnabled());
    assert.equal(await p.getByLabel('Button 1 brightness',{exact:true}).inputValue(),'255');
    await p.getByLabel('Button 1 control',{exact:true}).selectOption('unassigned');
    await p.locator('#kpG1').waitFor({state:'detached'});
    assert.equal(await p.locator('#kpG1').count(),0);
    assert.deepEqual(await p.evaluate(()=>keypadChanges(40003)),[{slot:1,mode:'unassigned'}]);
    assert(!s.calls.some(c=>c.path==='/api/keypad/save'));
  },true);
  await scenario('keypad-ha-readiness-and-capability-gates',async(p,s)=>{
    await keypad(p,s);
    await p.getByLabel('Button 1 control',{exact:true}).selectOption('ha');
    for(const [status,phrase] of [['disabled','Set it up in Settings'],['disconnected','until it reconnects'],['not_exposed','Choose Add to Home Assistant below']]){
      s.snapshot.controllers[0].ha_status=status;
      Object.assign(s.snapshot.status,{mqtt_enabled:status!=='disabled',mqtt_ready:status==='not_exposed'});
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
      await p.getByText(new RegExp(phrase)).first().waitFor();
      assert.equal(await p.getByRole('button',{name:'Save 1 change',exact:true}).isEnabled(),status==='not_exposed');
    }
    s.snapshot.controllers[0].slots[0].ha_available=false;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.waitForFunction(()=>document.querySelector('.kp-edit button').disabled);
    assert(await p.getByRole('button',{name:'Save 1 change',exact:true}).isDisabled());
    await p.evaluate(()=>saveKeypad(40003));
    await p.evaluate(()=>{S.caps.features.keypad_edit=false;render();});
    assert.equal(await p.locator('.kp-edit').count(),0);
    assert(!s.calls.some(c=>c.path==='/api/keypad/save'));
  });
  await scenario('keypad-ha-lost-response-blocks-draft-resubmit',async(p,s)=>{
    s.handlers['/api/keypad/save']=({route})=>route.abort('failed');
    await keypad(p,s);
    await p.getByLabel('Button 1 control',{exact:true}).selectOption('ha');
    await p.getByRole('button',{name:'Save 1 change',exact:true}).click();
    await p.getByText(/Save outcome not confirmed/).waitFor();
    assert(await p.getByLabel('Button 1 control',{exact:true}).isDisabled());
    assert(await p.getByRole('button',{name:'Save 1 change',exact:true}).isDisabled());
    assert(await p.getByRole('button',{name:'Review Operations',exact:true}).isVisible());
    await p.evaluate(async()=>{setKeypadChoice(40003,1,'mode','native');await saveKeypad(40003);await refreshSnapshot();});
    assert.equal(s.calls.filter(c=>c.path==='/api/keypad/save').length,1);
    assert(!s.calls.some(c=>c.path==='/api/keypad/read'));
  });
  await scenario('pairing-stops-after-failure',async(p,s)=>{
    s.handlers['/api/claim']=({send})=>send({error:'claim_verification_pending'},409);
    await p.evaluate(()=>{S.wiz=newWizard();S.wiz.step=1;S.wiz.lastRound=1;S.wiz.searchComplete=true;S.wiz.found=Object.fromEntries([1,2].map(n=>[n,{uuid:String(n).repeat(32),uuid_hash:n,hash_hex:'abcd'+n,first:n,lastRound:1,lastSeen:Date.now(),epoch:wizardDiscoveryEpoch(),detected_product_type:127,sel:true}]));return startClaims();});
    assert.equal(s.calls.filter(c=>c.path==='/api/claim').length,1);assert.equal(await p.evaluate(()=>S.wiz.claims[1].st),'skip');
  });
  await scenario('pairing-stops-after-review-and-double-click',async(p,s)=>{
    s.handlers['/api/claim']=({send})=>send({device_id:40004,requires_review:true});
    await p.evaluate(()=>{S.wiz=newWizard();S.wiz.step=1;S.wiz.lastRound=1;S.wiz.searchComplete=true;S.wiz.found=Object.fromEntries([1,2].map(n=>[n,{uuid:String(n).repeat(32),uuid_hash:n,hash_hex:'abcd'+n,first:n,lastRound:1,lastSeen:Date.now(),epoch:wizardDiscoveryEpoch(),detected_product_type:127,sel:true}]));return Promise.all([startClaims(),startClaims()]);});
    assert.equal(s.calls.filter(c=>c.path==='/api/claim').length,1);assert.equal(await p.evaluate(()=>S.wiz.claims[1].st),'skip');assert.equal(s.calls.find(c=>c.path==='/api/claim').body.setup_ha,false);
  });
  await scenario('remove-selection-preserves-other-members',async(p,s)=>{
    s.snapshot.inventory.groups[0].members=[40001,40002];
    await p.evaluate(data=>applySnapshot(data),clone(s.snapshot));await group(p);
    await p.getByRole('button',{name:'Remove lights',exact:true}).click();await p.getByLabel('Theatre 2',{exact:true}).check();await p.locator('#applyGroupMembers').click();
    await p.getByText('Selected lights have verified physical membership.').waitFor();
    assert.deepEqual(s.calls.find(c=>c.path==='/api/rooms/membership').body,{targets:[40002],group:300,member:false});assert.deepEqual(s.snapshot.inventory.groups[0].members,[40001]);
  });
  await scenario('membership-double-click-and-refresh-failure',async(p,s)=>{
    s.handlers['/api/rooms/membership']=async({send})=>{await new Promise(r=>setTimeout(r,150));s.snapshotFail=true;return send({physical_memberships_verified:true,message:'Membership verified.',rows:[{target:40001,status:'verified'}]});};
    await group(p);await p.getByRole('button',{name:'Add lights',exact:true}).click();await p.getByLabel('Theatre 1',{exact:true}).check();
    await p.evaluate(()=>Promise.all([applyGroupMembers(),applyGroupMembers()]));
    await p.getByText(/Membership verified. The view could not refresh/).waitFor();assert.equal(s.calls.filter(c=>c.path==='/api/rooms/membership').length,1);
  });
  for (const mobile of [false,true]) await scenario('recent-discovery-selectable-'+(mobile?'mobile':'desktop'),async(p,s)=>{
    let round=0;
    const candidate={uuid:'1122334455661400000a000000000001',uuid_hash:123,identity:{candidate_model:134,vendor:20,product:10}};
    s.handlers['/api/discover']=({send})=>send({devices:++round===1?[candidate]:[],candidates:[{...candidate,age_ms:round*5000,fresh:round===1}]});
    s.handlers['/api/claim']=({send})=>send({error:'claim_not_started',association_started:false,message:'Fresh target not heard. No pairing was started.'},409);
    await p.evaluate(()=>{S.wiz=newWizard();go('add');return startSearch();});
    assert.equal(round,3);
    const select=p.getByRole('checkbox',{name:'Select 0000007b',exact:true});assert(await select.isEnabled());assert(await select.isChecked());
    await p.evaluate(()=>{wizRows()[0].lastSeen=Date.now()-45000;render();});
    await p.getByText(/^Seen recently — checked again before pairing/).waitFor();
    assert(await select.isEnabled());assert(await p.getByRole('button',{name:'Pair 1 device',exact:true}).isEnabled());
    assert((await p.locator('body').innerText()).includes('0 seen in the last 30 seconds. 1 seen recently'));
    assert(!(await p.locator('body').innerText()).includes('1 unpaired device found'));
    const pair=p.getByRole('button',{name:'Pair 1 device',exact:true});await pair.scrollIntoViewIfNeeded();
    assert(await pair.evaluate(e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}),'Recent result Pair action is covered');
    await p.screenshot({path:path.join(evidence,'recent-discovery-'+(mobile?'mobile':'desktop')+'.png')});
    await p.evaluate(()=>{wizRows()[0].lastSeen=Date.now()-300001;render();});
    assert(await select.isDisabled());assert(await p.getByRole('button',{name:'Pair 0 devices',exact:true}).isDisabled());
    await p.evaluate(()=>{wizRows()[0].lastSeen=Date.now()-45000;render();});
    await p.getByRole('button',{name:'Pair 1 device',exact:true}).click();await p.getByRole('button',{name:'Pair',exact:true}).click();
    await p.getByText('Fresh target not heard. No pairing was started.',{exact:true}).waitFor();
    const claims=s.calls.filter(c=>c.path==='/api/claim');assert.equal(claims.length,1);assert.equal(claims[0].body.uuid,candidate.uuid);
  },mobile);
  await scenario('controller-discovery-is-explicit',async(p,s)=>{
    s.handlers['/api/discover']=({send})=>send({devices:[{uuid:'11'.repeat(16),uuid_hash:123,identity:{candidate_model:127}}]});
    await p.evaluate(()=>{S.wiz=newWizard();return startSearch();});
    assert.equal(await p.evaluate(()=>wizRows().length),1);assert.equal(await p.evaluate(()=>wizRows()[0].sel),false);
    assert.equal(await p.evaluate(()=>rowReady(wizRows()[0])),true);
    await p.evaluate(()=>{S.status.bootstrap.needs_first_claim=true;});assert.equal(await p.evaluate(()=>rowReady(wizRows()[0])),false);
    assert(!s.calls.some(c=>c.path==='/api/claim'));
  });
  await scenario('fresh-accessory-native-group',async(p,s)=>{
    await accessory(p,s);
    s.snapshot.inventory.groups[0].members=[40001,40002];
    s.snapshot.controllers[0].actions.setup_native={enabled:true,reason:'available'};
    s.snapshot.controllers[0].accessory_native={state:'not_configured'};
    Object.assign(s.snapshot.status,{mqtt_enabled:false,mqtt_ready:false});
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByRole('button',{name:'Choose direct group',exact:true}).click();
    const select=p.getByLabel('Lighting group',{exact:true});
    await select.selectOption('300');
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    assert.equal(await select.inputValue(),'300');
    s.handlers['/api/controller/setup-native']=({body,send})=>{
      assert.deepEqual(body,{target:40003,group:300});
      s.snapshot.status.jobs[40003]={id:'native-one',state:'running'};
      s.snapshot.controllers[0].actions.setup_native={enabled:false,reason:'operation_in_progress'};
      return send({accepted:true,job_id:'native-one'},202);
    };
    await p.getByRole('button',{name:'Start direct setup',exact:true}).click();
    await p.getByText(/Setup accepted\. Wait for the button prompt/).waitFor();
    assert.equal(s.calls.filter(c=>c.path==='/api/controller/setup-native').length,1);
    assert(!s.calls.some(c=>c.path==='/api/controller/setup'));
    s.snapshot.status.jobs[40003]={id:'native-one',state:'completed',result:{transaction:'native-receipt',ready:true,mode:'native',group:300,native_behavior_observed:true,assignment_read_back:false}};
    s.snapshot.controllers[0].accessory_native={state:'configured',target:300,transaction:'native-receipt'};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText('Direct group control: Theatre',{exact:true}).waitFor();
    await p.getByText(/Direct group setup completed/).waitFor();
    assert(!await p.getByRole('button',{name:'Set up Home Assistant control',exact:true}).count());
    await p.screenshot({path:path.join(evidence,'native-dimmer.png'),fullPage:true});
  });
  const configuredDimmer = async(p,s,model,mode) => {
    await (model===81?anyplace:accessory)(p,s);
    s.snapshot.inventory.groups[0].members=[40001];
    s.snapshot.inventory.groups.push({group_id:301,name:'Study',members:[40002],protected:false},
      {group_id:24575,name:'Private HA event target',members:[],protected:true});
    const v=s.snapshot.controllers[0],group=mode==='ha'?24575:300;
    v.control_configuration={state:'configured',configured:true,mode,group,transaction:'initial-accepted',assignment_read_back:false};
    v.actions.configure_control={enabled:true,reason:'available'};
    v.observed_target={target:group,baseline:{target:group,observed_ms:100,target_generation:1},valid_for_ms:15000};
    s.handlers['/api/dimmer/preview']=({body,send})=>send({target:40003,mode:body.mode,
      group:body.mode==='ha'?24574:body.group,baseline:clone(v.observed_target.baseline),revision:'b'.repeat(64),valid_for_ms:15000});
    s.handlers['/api/dimmer/configure']=({body,send})=>{
      assert.equal(body.revision,'b'.repeat(64));assert.deepEqual(body.baseline,v.observed_target.baseline);
      s.snapshot.status.jobs[40003]={id:'mode-change',state:'running'};
      return send({accepted:true,job_id:'mode-change'},202);
    };
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByRole('button',{name:'Change control mode or group',exact:true}).waitFor();
  };
  await scenario('configured-accessory-native-group-change-preserves-dropdown',async(p,s)=>{
    await configuredDimmer(p,s,91,'native');
    Object.assign(s.snapshot.status,{mqtt_enabled:false,mqtt_ready:false});
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByRole('button',{name:'Change control mode or group',exact:true}).click();
    await p.getByLabel('Lighting group',{exact:true}).selectOption('301');
    const select=p.getByLabel('Lighting group',{exact:true});await select.focus();await select.press('Space');
    await p.evaluate(()=>window.reviewGroupNode=document.querySelector('#dimmerControlGroup'));
    for(let i=0;i<3;i++){
      s.snapshot.controllers[0].observed_target.valid_for_ms-=100;
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));await p.waitForTimeout(180);
      assert(await p.evaluate(()=>window.reviewGroupNode===document.querySelector('#dimmerControlGroup')));
      assert.equal(await select.inputValue(),'301');
    }
    await p.keyboard.press('Escape');
    if(!await p.locator('#dimmerControlMode').count()){
      // Escape may close the native popup and the dialog on some platforms.
      await p.getByRole('button',{name:'Change control mode or group',exact:true}).click();
      await p.getByLabel('Lighting group',{exact:true}).selectOption('301');
    }
    assert(!await p.locator('#dimmerControlGroup option[value="24575"]').count());
    await p.getByRole('button',{name:'Review change',exact:true}).click();
    await p.waitForFunction(()=>!document.querySelector('#dimmerControlApply').disabled);
    await p.screenshot({path:path.join(evidence,'configured-native-group-review.png'),fullPage:true});
    await p.getByRole('button',{name:'Confirm change',exact:true}).click();
    await p.getByText(/Waiting for the dimmer/).waitFor();
    assert.deepEqual(s.calls.find(c=>c.path==='/api/dimmer/configure').body,
      {target:40003,mode:'native',group:301,baseline:{target:300,observed_ms:100,target_generation:1},revision:'b'.repeat(64)});
    assert.equal(s.calls.filter(c=>c.path==='/api/dimmer/configure').length,1);
    assert(!s.calls.some(c=>c.path==='/api/controller/setup'));
  });
  for (const mobile of [false, true]) {
    await scenario('imported-accessory-ha-only-' + (mobile ? 'mobile' : 'desktop'), async(p,s)=>{
      await configuredDimmer(p,s,91,'native');
      const v=s.snapshot.controllers[0];
      v.imported_ha_control=true;
      v.control_configuration={state:'not_configured',configured:false,mode:null,group:null};
      v.observed_target=null;
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
      await p.getByRole('button',{name:'Switch to Home Assistant only'}).click();
      assert(!await p.getByLabel('Lighting group',{exact:true}).isVisible());
      assert(!await p.getByRole('button',{name:'Set up direct group'}).count());
      assert(await p.getByRole('button',{name:'Review change'}).isDisabled());
      assert(await p.getByRole('button',{name:'Confirm change'}).isDisabled());
      assert(!s.calls.some(c=>c.path==='/api/dimmer/configure'));
      v.observed_target={target:270,baseline:{target:270,observed_ms:100,target_generation:1},valid_for_ms:15000};
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
      await p.getByRole('button',{name:'Review change'}).click();
      await p.getByText(/replace native target #270/).waitFor();
      await p.getByRole('button',{name:'Confirm change'}).click();
      await p.getByText(/Waiting for the dimmer/).waitFor();
      assert.deepEqual(s.calls.filter(c=>c.path==='/api/dimmer/configure').map(c=>c.body),
        [{target:40003,mode:'ha',baseline:v.observed_target.baseline,revision:'b'.repeat(64)}]);
      assert(!s.calls.some(c=>c.path==='/api/controller/setup' || c.path==='/api/claim'));
    },mobile);
  }
  await scenario('configured-accessory-native-to-ha-explicit-observation-acceptance',async(p,s)=>{
    await configuredDimmer(p,s,91,'native');
    await p.getByRole('button',{name:'Change control mode or group',exact:true}).click();
    await p.getByLabel('Control mode',{exact:true}).selectOption('ha');
    assert(!await p.getByLabel('Lighting group',{exact:true}).isVisible());
    await p.getByRole('button',{name:'Review change',exact:true}).click();
    await p.waitForFunction(()=>!document.querySelector('#dimmerControlApply').disabled);
    await p.getByRole('button',{name:'Confirm change',exact:true}).click();
    await p.getByText(/Waiting for the dimmer/).waitFor();
    const request=s.calls.find(c=>c.path==='/api/dimmer/configure').body;
    assert.equal(request.mode,'ha');assert(!Object.hasOwn(request,'group'));
    s.snapshot.status.jobs[40003]={id:'mode-change',state:'completed',result:{transaction:'mode-receipt',mode:'ha',group:24574,
      desired_behavior_observed:true,acceptance_required:true,assignment_read_back:false}};
    s.snapshot.status.pending_transactions=1;
    s.snapshot.controllers[0].control_configuration.state='needs_review';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/new control target was observed/).waitFor();
    assert(!s.calls.some(c=>c.path==='/api/dimmer/accept'),'acceptance must remain explicit');
    s.snapshot.status.pending_transactions=0;
    s.snapshot.controllers[0].control_configuration={configured:true,state:'configured',mode:'ha',group:24574,transaction:'mode-receipt',assignment_read_back:false};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText('Home Assistant control',{exact:true}).waitFor();
    await p.getByText('Control change accepted. Test On, Off and dimming.',{exact:true}).waitFor();
    assert(await p.getByRole('button',{name:'Change control mode or group',exact:true}).isEnabled());
  });
  await scenario('configured-anyplace-ha-to-native-mobile-wake',async(p,s)=>{
    await configuredDimmer(p,s,81,'ha');
    s.snapshot.controllers[0].anyplace_ha={state:'configured',target:24575,transaction:'initial-accepted',experimental:true};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByRole('button',{name:'Change control mode or group',exact:true}).click();
    await p.getByLabel('Control mode',{exact:true}).selectOption('native');
    await p.getByLabel('Lighting group',{exact:true}).selectOption('301');
    assert(await p.getByText(/Switching to direct control does not disable an existing HA automation/).isVisible());
    assert(await p.getByText(/small Wake-Up button on the bottom once immediately/).isVisible());
    await p.getByRole('button',{name:'Review change',exact:true}).click();
    await p.waitForFunction(()=>!document.querySelector('#dimmerControlApply').disabled);
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await p.screenshot({path:path.join(evidence,'anyplace-mode-review-mobile.png'),fullPage:true});
    await p.getByRole('button',{name:'Confirm change',exact:true}).click();
    await p.getByText(/Wake the Anyplace dimmer now/).waitFor();
    assert.equal(s.calls.filter(c=>c.path==='/api/dimmer/configure').length,1);
    assert.equal(s.calls.find(c=>c.path==='/api/dimmer/configure').body.mode,'native');
  },true);
  await scenario('configured-dimmer-preview-expiry-and-restart-send-nothing',async(p,s)=>{
    await configuredDimmer(p,s,91,'ha');
    await p.getByRole('button',{name:'Change control mode or group',exact:true}).click();
    await p.getByLabel('Control mode',{exact:true}).selectOption('native');
    await p.getByLabel('Lighting group',{exact:true}).selectOption('301');
    await p.getByRole('button',{name:'Review change',exact:true}).click();
    await p.waitForFunction(()=>!document.querySelector('#dimmerControlApply').disabled);
    await p.evaluate(()=>{dimmerControlReview.deadline=Date.now()-1;confirmDimmerControl();});
    assert(await p.getByText(/review changed or expired/).isVisible());
    assert(!s.calls.some(c=>c.path==='/api/dimmer/configure'));
    await p.getByRole('button',{name:'Review change',exact:true}).click();
    await p.waitForFunction(()=>!document.querySelector('#dimmerControlApply').disabled);
    s.snapshot.status.instance='restarted';await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    assert(await p.getByText(/bridge restarted. Close this form/).isVisible());
    await p.evaluate(()=>confirmDimmerControl());assert(!s.calls.some(c=>c.path==='/api/dimmer/configure'));
  });
  await scenario('configured-dimmer-lost-response-locks-mode-editor',async(p,s)=>{
    await configuredDimmer(p,s,91,'native');
    s.handlers['/api/dimmer/configure']=({route})=>route.abort('failed');
    await p.getByRole('button',{name:'Change control mode or group',exact:true}).click();
    await p.getByLabel('Control mode',{exact:true}).selectOption('ha');
    await p.getByRole('button',{name:'Review change',exact:true}).click();
    await p.waitForFunction(()=>!document.querySelector('#dimmerControlApply').disabled);
    await p.getByRole('button',{name:'Confirm change',exact:true}).click();
    await p.getByText(/Setup outcome is not confirmed/).waitFor();
    assert(await p.getByRole('button',{name:'Change control mode or group',exact:true}).isDisabled());
    await p.evaluate(()=>openDimmerControl(40003));assert(!await p.locator('#dimmerControlMode').count());
    assert.equal(s.calls.filter(c=>c.path==='/api/dimmer/configure').length,1);
  });
  await scenario('configured-dimmer-failed-job-recovers-with-matching-accepted-receipt',async(p,s)=>{
    await configuredDimmer(p,s,91,'native');
    await p.getByRole('button',{name:'Change control mode or group',exact:true}).click();
    await p.getByLabel('Control mode',{exact:true}).selectOption('ha');
    await p.getByRole('button',{name:'Review change',exact:true}).click();
    await p.waitForFunction(()=>!document.querySelector('#dimmerControlApply').disabled);
    await p.getByRole('button',{name:'Confirm change',exact:true}).click();
    await p.getByText(/Waiting for the dimmer/).waitFor();
    s.snapshot.status.jobs[40003]={id:'mode-change',state:'needs_review',result:{transaction:'timeout-receipt',mode:'ha',group:24574}};
    s.snapshot.status.pending_transactions=1;s.snapshot.controllers[0].control_configuration.state='needs_review';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/Setup is not confirmed/).waitFor();
    assert.equal(await p.evaluate(()=>accessoryRequests.get(40003).transaction),'timeout-receipt');
    s.snapshot.status.pending_transactions=0;s.snapshot.status.jobs[40003]={id:'recovery-job',state:'completed'};
    s.snapshot.controllers[0].control_configuration={state:'configured',configured:true,mode:'ha',group:24574,transaction:'other-receipt',assignment_read_back:false};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    assert(await p.getByRole('button',{name:'Change control mode or group',exact:true}).isDisabled());
    s.snapshot.controllers[0].control_configuration.transaction='timeout-receipt';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText('Control change accepted. Test On, Off and dimming.',{exact:true}).waitFor();
    assert(await p.getByRole('button',{name:'Change control mode or group',exact:true}).isEnabled());
    assert.equal(s.calls.filter(c=>c.path==='/api/dimmer/configure').length,1);
  });
  await scenario('battery-per-button-native-to-ha-uses-reviewed-save',async(p,s)=>{
    await keypad(p,s);s.snapshot.inventory.devices[2].product_id=126;s.snapshot.controllers[0].product_id=126;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/Battery keypad: follow each wake prompt/).waitFor();
    await p.getByLabel('Button 1 control',{exact:true}).selectOption('ha');
    s.handlers['/api/keypad/save']=({body,send})=>{assert.deepEqual(body.changes,[{slot:1,mode:'ha'}]);return send({accepted:true,job_id:'battery-mode'},202);};
    await p.getByRole('button',{name:'Save 1 change',exact:true}).click();
    await p.getByText(/Save accepted/).waitFor();
    assert.equal(s.calls.filter(c=>c.path==='/api/keypad/save').length,1);
    assert(!s.calls.some(c=>c.path==='/api/controller/setup'));
  });
  for (const model of [126,81]) await scenario('sleeping-claim-recovery-wake-progress-'+model,async(p,s)=>{
    const receipt={id:'sleeping-claim',kind:'claim',target:40005,status:'uncertain',
      detail:{product_id:model,uuid_hash:12345,association_complete:true,inventory_complete:true},
      recovery:{actions:['reconcile_claim'],reason:'authenticate_reserved_address_after_uncertain_association'}};
    s.snapshot.status.pending_transactions=1;
    s.handlers['/api/transactions']=({send})=>send({transactions:[receipt]});
    let finish;
    s.handlers['/api/reconcile']=({body,send})=>new Promise(resolve=>{
      assert.deepEqual(body,{transaction:'sleeping-claim'});
      finish=async()=>{receipt.status='verified';receipt.recovery={actions:[],reason:'terminal_receipt_retained'};
        s.snapshot.status.pending_transactions=0;
        await send({verified:true,associated:true,device_id:40005,association_retried:false,setup_automatically_resumed:false});resolve();};
    });
    await p.evaluate(snapshot=>{applySnapshot(snapshot);go('operations');},clone(s.snapshot));
    await p.getByRole('button',{name:'Check whether pairing finished',exact:true}).click();
    await p.waitForFunction(()=>Ops.busy==='sleeping-claim');
    const apply=async phase=>{
      s.snapshot.status.claim_progress={40005:{phase,uuid_hash:12345}};
      await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    };
    await apply('waiting_for_wake');
    const wake=p.locator('.ops-card [role=status]');
    await wake.filter({hasText:model===126?'Power/Off':'Wake-Up'}).waitFor();
    assert(await p.getByRole('button',{name:'Check whether pairing finished',exact:true}).isDisabled());
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    s.snapshot.status.claim_progress[40005].uuid_hash=999;
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await wake.filter({hasText:'Starting pairing verification'}).waitFor();
    assert(!/Power\/Off|Wake-Up/.test(await wake.innerText()),'wrong identity cannot prompt a wake');
    await apply('settling');await wake.filter({hasText:/[Ww]ake|[Ss]ettl/}).waitFor();
    await apply('verifying');await wake.filter({hasText:/[Cc]heck|[Vv]erif/}).waitFor();
    await finish();
    await p.getByRole('heading',{name:'Needs review (0)',exact:true}).waitFor();
    await p.getByRole('button',{name:'Show history',exact:true}).click();
    await p.locator('.ops-card [role=status]').filter({hasText:/[Pp]airing.*verified/}).waitFor();
    assert.equal(s.calls.filter(c=>c.path==='/api/reconcile').length,1);
    assert(!s.calls.some(c=>['/api/claim','/api/controller/setup','/api/claim/reset-reconcile'].includes(c.path)));
  },true);
  for (const mobile of [false,true]) for (const verified of [true,false]) {
    await scenario(`removal-automatic-verification-${verified?'success':'review'}-${mobile?'mobile':'desktop'}`,async(p,s)=>{
      s.snapshot.status.capabilities={unclaim:true};
      await p.evaluate(snapshot=>{applySnapshot(snapshot);openDrawer('device',40001);},clone(s.snapshot));
      s.handlers['/api/unclaim/preview']=({send})=>send({target:40001,revision:'a'.repeat(64),eligible:true,enabled:true,
        affected_groups:[],native_dependencies:[]});
      let finish;
      const pending = new Promise(resolve=>{finish=resolve;});
      s.handlers['/api/unclaim']=async({send})=>{
        await pending;
        if (verified) {
          s.snapshot.inventory.devices=s.snapshot.inventory.devices.filter(d=>d.device_id!==40001);
          s.snapshot.status.inventory_revision='r2';
        } else s.snapshot.status.pending_transactions=1;
        return send({removed:verified,transaction:'remove-one',verification:verified?'verified':'needs_review',
          verification_attempts:3,write_retried:false,requires_review:!verified,publication_pending:false});
      };
      const remove=p.getByRole('button',{name:'Remove from mesh…',exact:true});await remove.click();
      await p.getByText('Sending removal and verifying unpaired status…',{exact:true}).waitFor();
      assert(await remove.isDisabled());await p.evaluate(()=>removeDevice(40001));
      assert.equal(s.calls.filter(c=>c.path==='/api/unclaim').length,1);
      assert(s.dialogs[0].includes('checks automatically'));
      await p.screenshot({path:path.join(evidence,`removal-verifying-${mobile?'mobile':'desktop'}.png`),fullPage:true});
      finish();
      if (verified) {
        await p.waitForFunction(()=>!S.devices.some(d=>d.avion_id===40001));
      } else {
        await p.getByText(/automatic verification did not confirm/).waitFor();
        assert(await remove.isDisabled());
        assert(await p.getByRole('button',{name:'Review this operation',exact:true}).isVisible());
      }
      assert.equal(s.calls.filter(c=>c.path==='/api/unclaim').length,1);
      assert(!s.calls.some(c=>c.path==='/api/reconcile'), 'Verification belongs to the bridge, not a second browser request');
    },mobile);
  }
  await scenario('removal-impact-preview-lost-response-locks-repeat',async(p,s)=>{
    s.snapshot.status.capabilities={unclaim:true};
    await p.evaluate(snapshot=>{applySnapshot(snapshot);openDrawer('device',40001);},clone(s.snapshot));
    s.handlers['/api/unclaim/preview']=({send})=>send({target:40001,revision:'a'.repeat(64),eligible:true,enabled:true,
      affected_groups:[{group_id:300,name:'Theatre',members:[40001,40002]}],native_dependencies:[{controller:40003,destination:300,direct:false}]});
    s.handlers['/api/unclaim']=({body,send})=>{assert.deepEqual(body,{target:40001,revision:'a'.repeat(64)});return send({error:'response_lost'},504);};
    const remove=p.getByRole('button',{name:'Remove from mesh…',exact:true});await remove.click();
    await p.getByText(/Removal outcome not confirmed/).waitFor();
    assert(s.dialogs[0].includes('Theatre (1 light remaining)'));
    assert(s.dialogs[0].includes('Native controller assignments'));
    assert(s.dialogs[0].includes('Home Assistant automations have not been checked'));
    assert(await remove.isDisabled());await p.evaluate(()=>removeDevice(40001));
    assert.equal(s.calls.filter(c=>c.path==='/api/unclaim').length,1);
    assert.equal(s.calls.filter(c=>c.path==='/api/unclaim/preview').length,1);
  },true);
  await scenario('removal-preview-restart-never-dispatches',async(p,s)=>{
    s.snapshot.status.capabilities={unclaim:true};
    await p.evaluate(snapshot=>{applySnapshot(snapshot);openDrawer('device',40001);},clone(s.snapshot));
    s.handlers['/api/unclaim/preview']=async({send})=>{
      s.snapshot.status.instance='two';await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
      return send({target:40001,revision:'a'.repeat(64),eligible:true,enabled:true,affected_groups:[],native_dependencies:[]});};
    await p.getByRole('button',{name:'Remove from mesh…',exact:true}).click();
    await p.getByText(/Removal not started.*bridge restarted/).waitFor();
    assert(!s.calls.some(c=>c.path==='/api/unclaim'));assert.equal(s.dialogs.length,0);
  });
  await scenario('removal-confirmed-publication-recovery-is-metadata-only',async(p,s)=>{
    const receipt={id:'removed-one',kind:'remove',target:40001,status:'verified',detail:{publication_pending:true},
      recovery:{actions:['refresh_removed_publication'],reason:'removal_publication_pending'}};
    s.handlers['/api/transactions']=({send})=>send({transactions:[receipt]});
    s.handlers['/api/reconcile']=({body,send})=>{assert.deepEqual(body,{transaction:'removed-one'});
      receipt.detail.publication_pending=false;receipt.recovery={actions:[],reason:'terminal_receipt_retained'};
      return send({removed:true,address_retired:true,publication_pending:false,write_retried:false});};
    await p.evaluate(()=>go('operations'));
    await p.getByRole('heading',{name:'Needs review (1)',exact:true}).waitFor();
    await p.getByRole('button',{name:'Refresh publication cleanup',exact:true}).click();
    await p.getByRole('heading',{name:'Needs review (0)',exact:true}).waitFor();
    assert(s.dialogs[0].includes('does not scan, reset, pair or send removal again'));
    assert.equal(s.calls.filter(c=>c.path==='/api/reconcile').length,1);
    assert(!s.calls.some(c=>['/api/unclaim','/api/discover','/api/claim'].includes(c.path)));
  },true);
  await scenario('timing-diagnostics-mobile-updates-preserve-expanded-details',async(p,s)=>{
    const trace={id:1,target:300,source:'mqtt',status:'waiting_reports',fields:['brightness'],members:2,members_reported:1,members_matched:1,queue_ms:12,dispatch_ms:4,first_report_ms:180,all_reports_ms:null,mqtt_sent_ms:205,mqtt_queue_ms:5,mqtt_publish_call_ms:20};
    s.snapshot.status.command_timings={recent:[trace]};
    await p.evaluate(snapshot=>{applySnapshot(snapshot);go('settings');},clone(s.snapshot));
    await p.getByRole('heading',{name:'Control response times',exact:true}).waitFor();
    await p.locator('.command-timings summary').click();
    assert(await p.getByText('180 ms',{exact:true}).isVisible());
    assert(await p.getByText(/does not measure when Home Assistant displays/).isVisible());
    trace.first_report_ms=190;trace.all_reports_ms=340;trace.members_matched=2;trace.status='reported';
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText('190 ms',{exact:true}).waitFor();
    assert(await p.locator('.command-timings details').evaluate(el=>el.open));
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    assert(!s.calls.some(c=>c.body));
    await p.screenshot({path:path.join(evidence,'timing-mobile.png'),fullPage:true});
  },true);
  await scenario('removal-publication-cleanup-visible-with-no-uncertain-write',async(p,s)=>{
    s.snapshot.status.pending_transactions=0;s.snapshot.status.publication_pending=1;
    await p.evaluate(snapshot=>{applySnapshot(snapshot);go('home');},clone(s.snapshot));
    await p.getByText('1 removed device needs Home Assistant cleanup',{exact:true}).waitFor();
    assert(await p.getByRole('button',{name:'Review cleanup',exact:true}).isVisible());
    await p.evaluate(()=>go('settings'));
    assert(await p.getByText('Publication cleanup',{exact:true}).isVisible());
    assert(!s.calls.some(c=>c.body));
  });
  await scenario('battery-cached-mappings-enable-description-not-editing',async(p,s)=>{
    await battery(p,s,false);
    s.snapshot.controllers[0].cached_mapping={status:'cached',assignment_read_back:false,slots:[{slot:1,target:24575,preset:255},{slot:2,target:24574,preset:255}]};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText('Using saved button mappings.',{exact:true}).waitFor();
    assert(await p.getByText(/These assignments have not been read in this connection/).isVisible());
    assert(await p.getByText('Stored assignments have not been read back.',{exact:true}).isVisible());
    assert.equal(await p.getByLabel('Button 1 control',{exact:true}).count(),0);
    assert(!s.calls.some(c=>c.path==='/api/controller/setup'||c.path==='/api/keypad/read'||c.path==='/api/keypad/save'));
    s.snapshot.controllers[0].cached_mapping={status:'revoked',slots:[]};
    await p.evaluate(snapshot=>applySnapshot(snapshot),clone(s.snapshot));
    await p.getByText(/No usable saved button mapping/).waitFor();
  },true);
  await scenario('mobile-groups',async(p)=>{await group(p);assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await p.screenshot({path:path.join(evidence,'mobile-groups.png'),fullPage:true});},true);
  await browser.close();const output={results,errors};fs.writeFileSync(path.join(evidence,'browser.json'),JSON.stringify(output,null,2)+'\n');console.log(JSON.stringify(output,null,2));if(errors.length||results.some(r=>!r.passed))process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1;});
