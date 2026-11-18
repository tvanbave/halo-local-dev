/* Offline browser QA. All requests are intercepted; no bridge is contacted. */
const fs = require('fs'), path = require('path'), assert = require('assert');
const pw = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const engine = process.env.BROWSER_ENGINE || 'chromium';
const root = path.resolve(__dirname, '..');
const evidence = process.env.EVIDENCE_DIR || '/tmp/dev40-form-audit';
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
  async function scenario(name, test, mobile=false) {
    if(process.env.SCENARIO_PATTERN && !new RegExp(process.env.SCENARIO_PATTERN).test(name))return;
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
        const g=state.snapshot.inventory.groups[0];g.members=body.member?[...new Set([...g.members,...body.targets])]:g.members.filter(t=>!body.targets.includes(t));
        return send({physical_memberships_verified:true,message:'Selected lights have verified physical membership.',rows:body.targets.map(target=>({target,status:'verified'}))});
      }
      if(url.pathname==='/api/settings/mqtt')return send({revision:'m1',enabled:false,host:'broker.example.invalid',port:1883,username:'owner',tls:false,password_set:true});
      if(url.pathname==='/api/keypad/read')return send({accepted:true,completed:false,job_id:'read-one'},202);
      return send({error:'unexpected_mock_request'},404);
    });
    try{await page.goto('http://synthetic.invalid');await page.getByRole('button',{name:'Groups',exact:true}).waitFor();await page.waitForFunction(()=>S.synced);await test(page,state);results.push({name,passed:true});}
    catch(e){results.push({name,passed:false,error:e.message});await page.screenshot({path:path.join(evidence,name+'-failure.png'),fullPage:true});}
    finally{await context.close();}
  }

  async function stable(p, selector, {text=null, select=false, range=true}={}) {
    const f=p.locator(selector);await f.waitFor();
    range=range&&await f.evaluate(el=>['text','search','password','url','tel'].includes(el.type));
    if(text!==null)await f.fill(text);else await f.focus();
    const handle=await f.elementHandle();
    if(range)await f.evaluate(el=>{if(el.setSelectionRange&&el.type!=='number')el.setSelectionRange(1,3,'backward');});
    await p.evaluate(selector=>{
      window.auditField=document.querySelector(selector);window.auditDetached=false;
      window.auditObserver?.disconnect();
      window.auditObserver=new MutationObserver(rows=>{for(const r of rows)for(const n of r.removedNodes)if(n===auditField||n.contains?.(auditField))auditDetached=true;});
      auditObserver.observe(document.body,{childList:true,subtree:true});
    },selector);
    if(select)await f.evaluate(el=>el.blur());
    for(let i=0;i<3;i++){await p.evaluate(()=>render(true));await p.waitForTimeout(160);}
    assert(await handle.evaluate(el=>el.isConnected&&el===document.getElementById(el.id)),'Field node replaced: '+selector);
    assert.equal(await p.evaluate(()=>auditDetached),false,'Field detached during update: '+selector);
    if(text!==null)assert.equal(await f.inputValue(),text);
    if(range)assert.deepEqual(await f.evaluate(el=>el.type==='number'?null:[el.selectionStart,el.selectionEnd,el.selectionDirection]),[1,3,'backward']);
  }
  const light=async p=>{await p.evaluate(()=>openDrawer('device',40001));};
  const settings=async p=>{await p.evaluate(()=>go('settings'));await p.locator('[data-settings-panel=mqtt] summary').click();await p.locator('[data-settings-panel=cloud] summary').click();await p.locator('#mqttHost:enabled').waitFor();};
  await scenario('search-field-keeps-node-selection-and-live-results',async p=>{
    await p.evaluate(()=>go('devices'));await stable(p,'#devSearch',{text:'Theatre'});
    await p.locator('#devSearch').fill('missing');await p.getByText(/No devices match/).waitFor();
    await p.locator('#devSearch').fill('Theatre');assert.equal(await p.locator('tbody tr').count(),2);
  });
  await scenario('settings-all-text-fields-stay-connected',async p=>{
    await settings(p);await p.locator('#mqttPasswordAction').selectOption('replace');
    for(const [id,text] of [['mqttHost','new.example.invalid'],['mqttUsername','draft-user'],['mqttPassword','synthetic-draft'],['cloudEmail','draft@example.invalid'],['cloudPassword','synthetic-cloud']])await stable(p,'#'+id,{text});
    await stable(p,'#mqttPort',{text:'1884',range:false});
  },true);
  await scenario('wizard-new-room-input-keeps-node-and-selection',async p=>{
    await p.evaluate(()=>{S.wiz=newWizard();go('add');});await p.locator('#wRoom').selectOption('__new');
    await stable(p,'#wrnnull',{text:'Family room'});
  },true);
  await scenario('wizard-pairing-name-keeps-node-and-selection',async p=>{
    await p.evaluate(()=>{S.wiz=newWizard();Object.assign(S.wiz,{step:3,claims:[{id:40001,name:'Theatre 1',st:'done',type:93}],names:{40001:'Theatre 1'}});go('add');});
    await stable(p,'#wn40001',{text:'Library lamp'});
  },true);
  await scenario('drawer-new-room-draft-survives-blur-and-snapshot',async p=>{
    await light(p);await p.locator('#dRoom').selectOption('__new');await p.locator('#newRoomName').fill('Office');
    await p.locator('.drawer-head h3').click();await p.evaluate(()=>render());
    assert(await p.locator('#newRoomName').isVisible(),'New room editor disappeared after blur');
    assert.equal(await p.locator('#newRoomName').inputValue(),'Office');
    assert.equal(await p.getByLabel('New room name',{exact:true}).count(),1,'New room input needs a label');
  },true);
  await scenario('modal-dropdown-escape-keeps-dialog',async(p,s)=>{
    Object.assign(s.snapshot.inventory.devices[2],{product_id:91,name:'Accessory'});
    s.snapshot.inventory.groups[0].members=[40001];s.snapshot.status.mqtt_enabled=s.snapshot.status.mqtt_ready=true;
    s.snapshot.controllers[0]={device_id:40003,product_id:91,actions:{setup_native:{enabled:true}},slots:[]};
    await p.evaluate(s=>{applySnapshot(s);openDrawer('device',40003);openAccessoryNativeSetup(40003);},clone(s.snapshot));
    await p.locator('#accessoryGroup').focus();await p.evaluate(()=>document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})));
    assert.equal(await p.locator('#modalRoot .modal').count(),1,'Escape dismissed the dialog while using its dropdown');
  });
  await scenario('modal-dropdown-phone-size-and-long-label',async(p,s)=>{
    Object.assign(s.snapshot.inventory.devices[2],{product_id:91,name:'Accessory'});
    Object.assign(s.snapshot.inventory.groups[0],{members:[40001],name:'A very long downstairs lighting group that should fit on a small screen'});
    s.snapshot.controllers[0]={device_id:40003,product_id:91,actions:{setup_native:{enabled:true}},slots:[]};
    await p.evaluate(s=>{applySnapshot(s);openDrawer('device',40003);openAccessoryNativeSetup(40003);},clone(s.snapshot));
    const metrics=await p.locator('#accessoryGroup').evaluate(el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return{font:parseFloat(s.fontSize),height:r.height,right:r.right,width:innerWidth};});
    assert(metrics.font>=16&&metrics.height>=44&&metrics.right<=metrics.width,JSON.stringify(metrics));
  },true);
  await scenario('modal-keyboard-focus-stays-inside',async p=>{
    await p.evaluate(()=>{go('groups');openCreateGroup();});
    await p.locator('#createGroupSubmit').focus();await p.keyboard.press('Tab');
    assert(await p.evaluate(()=>!!document.activeElement.closest('#modalRoot')),'Tab escaped behind modal');
  });
  await scenario('device-name-selection-survives-refresh',async p=>{
    await light(p);await stable(p,'#dName',{text:'Study lamp'});
  },true);
  await scenario('invalid-device-name-stays-visible-with-error',async p=>{
    await light(p);await p.locator('#dName').fill('   ');await p.locator('.drawer-head h3').click();
    await p.getByText('Enter a name with 1–80 printable characters.',{exact:true}).waitFor();
    assert.equal(await p.locator('#dName').inputValue(),'   ');
    assert.equal(await p.locator('#dName').getAttribute('aria-invalid'),'true');
    await p.getByRole('button',{name:'Discard changes',exact:true}).click();assert.equal(await p.locator('#dName').inputValue(),'Theatre 1');
  });
  await scenario('failed-name-save-keeps-draft-no-repeat',async(p,s)=>{
    s.handlers['/api/metadata']=({send})=>send({error:'unavailable'},503);
    await light(p);await p.locator('#dName').fill('Study lamp');await p.locator('.drawer-head h3').click();
    await p.getByText(/Name was not confirmed/).waitFor();await p.evaluate(()=>render());
    assert.equal(await p.locator('#dName').inputValue(),'Study lamp');assert.equal(s.calls.filter(c=>c.path==='/api/metadata').length,1);
  });
  await scenario('empty-group-name-inline-validation',async(p,s)=>{
    await p.evaluate(()=>{go('groups');openCreateGroup();});await p.locator('#createGroupSubmit').click();
    assert.equal(await p.locator('#newGroupName').getAttribute('aria-invalid'),'true');
    assert((await p.locator('#groupFormStatus').textContent()).includes('1–128'));
    assert.equal(s.calls.filter(c=>c.path==='/api/metadata').length,0);
    await stable(p,'#newGroupName',{text:'Office'});
  },true);
  await scenario('mqtt-port-required-no-save',async(p,s)=>{
    await settings(p);await p.locator('#mqttPort').fill('');await p.locator('#mqttSave').click();
    assert.equal(await p.locator('#mqttPort').evaluate(el=>el.validity.valueMissing),true);
    assert.equal(s.calls.filter(c=>c.path==='/api/settings/mqtt'&&c.body).length,0);
  });
  await scenario('bulk-name-text-preserved',async(p,s)=>{
    s.snapshot.inventory.devices[0].name=s.snapshot.inventory.devices[1].name='Duplicate';
    await p.evaluate(s=>{applySnapshot(s);openNamer();},clone(s.snapshot));await stable(p,'#nm40001',{text:'West lamp'});
  },true);
  await scenario('composition-keeps-draft-and-enter-does-not-submit',async(p,s)=>{
    await p.evaluate(()=>{S.wiz=newWizard();go('add');});await p.locator('#wRoom').selectOption('__new');
    await p.locator('#wrnnull').fill('書斎');
    await p.locator('#wrnnull').evaluate(el=>{el.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true}));});
    await stable(p,'#wrnnull',{range:false});assert.equal(await p.locator('#wrnnull').inputValue(),'書斎');
    await p.locator('#wrnnull').evaluate(el=>el.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true})));
    assert.equal(s.calls.filter(c=>c.body).length,0);
  },true);
  for(const model of [127,126])await scenario('keypad-'+model+'-mode-group-preset-stable',async(p,s)=>{
    s.features.keypad_edit=true;s.features.battery_keypad_setup=true;
    s.snapshot.inventory.devices[2].product_id=model;
    s.snapshot.controllers[0]={device_id:40003,product_id:model,assignment_read_back:true,revision:'rev1',
      actions:{read_assignments:{enabled:true},edit_assignment:{enabled:true}},
      slots:[1,2,3,4].map(slot=>({slot,target:300,preset:200,status:'verified_mapping',editable:true,ha_available:true}))};
    await p.evaluate(async s=>{S.caps=await api('/api/capabilities');applySnapshot(s);openDrawer('device',40003);},clone(s.snapshot));
    for(const id of ['kpM1','kpG1','kpL1']){
      const f=p.locator('#'+id);await f.focus();await f.press('Space');
      const node=await f.elementHandle();for(let n=0;n<3;n++){await p.evaluate(()=>render(true));await p.waitForTimeout(150);}
      assert(await node.evaluate(el=>el.isConnected));await p.keyboard.press('Escape');
    }
    await p.screenshot({path:path.join(evidence,'keypad-'+model+'.png'),fullPage:true});
  },true);
  await scenario('all-settings-fields-fit-320-and-short-landscape',async p=>{
    await settings(p);await p.locator('#mqttPasswordAction').selectOption('replace');
    for(const viewport of [{width:320,height:568},{width:667,height:375}]){
      await p.setViewportSize(viewport);
      const fields=p.locator('#bridgeSettingsMount input:not([type=checkbox]),#bridgeSettingsMount select');
      for(let n=0;n<await fields.count();n++){
        const f=fields.nth(n);if(!await f.isVisible())continue;
        const m=await f.evaluate(el=>{const r=el.getBoundingClientRect();return{id:el.id,font:parseFloat(getComputedStyle(el).fontSize),height:r.height,left:r.left,right:r.right,labels:el.labels.length};});
        assert(m.font>=16&&m.height>=44&&m.left>=0&&m.right<=viewport.width&&m.labels>0,JSON.stringify(m));
      }
      assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await p.screenshot({path:path.join(evidence,'settings-'+viewport.width+'.png'),fullPage:true});
    }
  },true);
  await scenario('unknown-device-type-picker-keeps-node',async p=>{
    await p.evaluate(()=>{S.wiz=newWizard();Object.assign(S.wiz,{step:1,lastRound:1,found:{123:{uuid_hash:123,hash_hex:'0000007b',detected_product_type:0,manual:0,seen:1,lastRound:1}}});go('add');});
    const f=p.getByRole('combobox',{name:'Choose type',exact:true});await f.focus();await f.press('Space');
    const node=await f.elementHandle();for(let n=0;n<3;n++){await p.evaluate(()=>render(true));await p.waitForTimeout(150);}
    assert(await node.evaluate(el=>el.isConnected));await p.keyboard.press('Escape');
    await f.selectOption('91');assert.equal(await p.evaluate(()=>S.wiz.found[123].manual),91);
  },true);
  await scenario('mobile-focused-settings-fields-clear-bottom-navigation',async p=>{
    await p.setViewportSize({width:390,height:520});await settings(p);await p.locator('#mqttPasswordAction').selectOption('replace');
    for(const id of ['mqttHost','mqttPort','mqttUsername','mqttPasswordAction','mqttPassword','cloudEmail','cloudPassword']){
      const f=p.locator('#'+id);await f.scrollIntoViewIfNeeded();await f.focus();
      const m=await f.evaluate(el=>{const r=el.getBoundingClientRect(),nav=document.querySelector('#nav').getBoundingClientRect();return{id:el.id,top:r.top,bottom:r.bottom,navTop:nav.top};});
      assert(m.top>=0&&m.bottom<=m.navTop,JSON.stringify(m));
    }
    await p.screenshot({path:path.join(evidence,'focused-cloud-password.png')});
  },true);
  await browser.close();fs.writeFileSync(path.join(evidence,engine+'-results.json'),JSON.stringify({engine,results,errors},null,2)+'\n');
  console.log(JSON.stringify({passed:results.filter(r=>r.passed).length,total:results.length,errors}));
  if(results.some(r=>!r.passed)||errors.length){console.log(JSON.stringify(results.filter(r=>!r.passed),null,2));process.exitCode=1;}
})().catch(e=>{console.error(e);process.exitCode=1;});
