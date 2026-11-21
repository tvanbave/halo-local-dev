/* Offline Settings acceptance. Every request is intercepted; no bridge or broker is contacted. */
const fs=require('fs'),path=require('path'),assert=require('assert');
const pw=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const engine=process.env.BROWSER_ENGINE||'chromium';
const root=path.resolve(__dirname,'..'),evidence=process.env.EVIDENCE_DIR||'/tmp/avionmesh-settings-browser-evidence';
const clone=x=>JSON.parse(JSON.stringify(x));
const initial={revision:'m1',enabled:false,host:'broker.example.invalid',port:8883,username:'owner',tls:true,password_set:true,connected:false,source:'settings'};
const initialSnapshot=()=>({status:{mesh_ready:true,mqtt_enabled:false,mqtt_ready:false,preview_only:false,pending_transactions:0,jobs:{},instance:'one',inventory_revision:'i1',bootstrap:{needs_first_claim:false}},inventory:{devices:[{device_id:40001,product_id:93,name:'Theatre 1'}],groups:[{group_id:300,name:'Theatre',members:[40001],protected:false}]},group_workflow:{programming_enabled:true,lights:[{target:40001,name:'Theatre 1',eligible:true}]},controllers:[],device_states:{}});
const ready=()=>({status:'ready',locations:[{id:'house',name:'Home',matching_mesh:true}],selected_location:{id:'house',name:'Home',matching_mesh:true},preview_id:'p1',revision:'i1',expires_in:300,can_apply:true,changes:{devices_added:[{device_id:40002,product_id:134,name:'Hall lamp',exposed:false}],groups_added:[{group_id:301,name:'Hall',members:[40002],exposed:false,protected:false}],members_added:[]},message:'Ready to import the reviewed metadata.',physical_memberships_changed:false,new_devices_exposed:false});
(async()=>{
 fs.mkdirSync(evidence,{recursive:true});const browser=await pw[engine].launch(engine==='chromium'?{headless:true,executablePath:process.env.CHROME_EXECUTABLE,args:['--disable-background-networking']}:{headless:true});
 const results=[],errors=[];
 async function scenario(name,test,mobile=false){
  const context=await browser.newContext({viewport:mobile?{width:390,height:844}:{width:1280,height:960},isMobile:mobile,hasTouch:mobile,serviceWorkers:'block'}),p=await context.newPage();p.setDefaultTimeout(5000);
  const s={mqtt:clone(initial),snapshot:initialSnapshot(),calls:[],handlers:{}};
  p.on('pageerror',e=>errors.push(name+': '+e.message));p.on('dialog',d=>d.accept());
  await context.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());if(u.origin!=='http://synthetic.invalid')return route.abort();
   const assets={'/':['ui.html','text/html'],'/ui.js':['ui.js','text/javascript'],'/settings.js':['settings.js','text/javascript'],'/backup.js':['backup.js','text/javascript'],'/ops.js':['ops.js','text/javascript'],'/ui.css':['ui.css','text/css']};
   if(assets[u.pathname]){const [file,kind]=assets[u.pathname];return route.fulfill({contentType:kind,body:fs.readFileSync(path.join(root,'avionmesh_pi/web',file),'utf8')});}
   let body;try{body=req.postDataJSON();}catch{}s.calls.push({path:u.pathname,method:req.method(),body});
   const send=(value,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
   const handler=s.handlers[req.method()+' '+u.pathname];if(handler)return handler({body,send,route});
   if(u.pathname==='/api/capabilities')return send({version:'0.5.0.dev16',features:{relay_settings:true,software_updates:true,control:true,labels:true,group_metadata:true,mqtt_settings:true,cloud_import:true},types:{lights:{93:'RL4'},controllers:{},color_temp:[93]}});
   if(u.pathname==='/api/snapshot')return send(s.snapshot);
   if(u.pathname==='/api/events')return route.fulfill({contentType:'text/event-stream',body:''});
   if(u.pathname==='/api/settings/mqtt'&&req.method()==='GET')return send(s.mqtt);
   if(u.pathname==='/api/settings/mqtt/test')return send({connected:true,published:false,subscribed:false,saved:false});
   if(u.pathname==='/api/settings/mqtt'){
    const {password,password_action,...publicFields}=body;s.mqtt={...s.mqtt,...publicFields,revision:'m2',password_set:password_action==='replace'||password_action==='keep'&&s.mqtt.password_set,connected:false};s.snapshot.status.mqtt_enabled=s.mqtt.enabled;return send({saved:true,applied:true,settings:s.mqtt,durability_confirmed:true});
   }
   if(u.pathname==='/api/cloud-import/preview')return send(ready());
   if(u.pathname==='/api/cloud-import/apply')return send({saved:true,revision:'i2',devices:2,groups:2,physical_memberships_changed:false,new_devices_exposed:false});
   return send({error:'unexpected_request'},404);
  });
  try{await p.goto('http://synthetic.invalid');await p.waitForFunction(()=>S.synced,null,{timeout:5000});await p.getByRole('button',{name:'Settings',exact:true}).click();assert(await p.locator('#mqttHost').isHidden());assert(await p.locator('#cloudEmail').isHidden());
  const operations=p.getByRole('button',{name:'Open operations'});
  await operations.waitFor({state:'visible'});
  if (mobile) {
    await p.waitForFunction(()=>{
      const button=Array.from(document.querySelectorAll('#main button')).find(el=>el.textContent.trim()==='Open operations');
      const rect=button?.getBoundingClientRect(),nav=document.querySelector('#nav')?.getBoundingClientRect();
      return rect?.height>0 && nav?.height>0 && rect.bottom<nav.top;
    });
  }
  await p.locator('[data-settings-panel=mqtt] summary').click();
  await p.locator('#mqttHost').waitFor();await p.waitForFunction(()=>document.querySelector('#mqttFields')?.disabled===false);await test(p,s);results.push({name,passed:true});}
  catch(error){results.push({name,passed:false,error:error.message});await p.screenshot({path:path.join(evidence,name+'-failure.png'),fullPage:true});}
  finally{await context.close();}
 }
 const credentials=async p=>{if(await p.locator('#cloudEmail').isHidden())await p.locator('[data-settings-panel=cloud] summary').click();await p.getByLabel('Cloud account email',{exact:true}).fill('owner@example.invalid');await p.getByLabel('Cloud account password',{exact:true}).fill('synthetic-cloud-password');};
 const preview=async p=>{await credentials(p);await p.getByRole('button',{name:'Preview cloud import',exact:true}).click();await p.getByRole('button',{name:'Import reviewed metadata',exact:true}).waitFor();};
 await scenario('mqtt-drafts-survive-snapshot-and-navigation',async(p,s)=>{
  await p.getByLabel('Broker address',{exact:true}).fill('changed.example.invalid');await p.getByLabel('Broker password',{exact:true}).selectOption('replace');await p.getByLabel('New password',{exact:true}).fill('synthetic-draft');
  await p.evaluate(()=>{S.status.receive_count=99;render();});assert.equal(await p.locator('#mqttHost').inputValue(),'changed.example.invalid');assert.equal(await p.locator('#mqttPassword').inputValue(),'synthetic-draft');assert.equal(await p.evaluate(()=>document.activeElement.id),'mqttPassword');
  await p.locator('#navItems button').filter({hasText:/^Home/}).click();await p.getByRole('button',{name:'Settings',exact:true}).click();assert.equal(await p.locator('#mqttPassword').inputValue(),'');assert.equal(await p.locator('#mqttHost').inputValue(),'changed.example.invalid');assert.equal(s.calls.filter(c=>c.path==='/api/settings/mqtt').length,1);
 });
 await scenario('mqtt-test-is-not-save-and-clears-password',async(p,s)=>{
  await p.getByLabel('Broker password',{exact:true}).selectOption('replace');await p.getByLabel('New password',{exact:true}).fill('synthetic-test');await p.getByRole('button',{name:'Test connection',exact:true}).click();await p.getByText(/Broker connection succeeded/).waitFor();
  assert.equal(await p.locator('#mqttPassword').inputValue(),'');assert(!await p.getByLabel('Enable MQTT',{exact:true}).isChecked());assert.equal(s.calls.filter(c=>c.path==='/api/settings/mqtt'&&c.method==='POST').length,0);const call=s.calls.find(c=>c.path==='/api/settings/mqtt/test');assert.equal(call.body.enabled,true);assert.equal(call.body.password,'synthetic-test');assert(await p.getByText(/Re-enter the new password/).isVisible());
 });
 await scenario('mqtt-save-and-disable-use-reviewed-revisions',async(p,s)=>{
  await p.getByLabel('Enable MQTT',{exact:true}).check();await p.getByRole('button',{name:'Save MQTT settings',exact:true}).click();await p.getByText(/MQTT settings saved/).waitFor();
  const first=s.calls.find(c=>c.path==='/api/settings/mqtt'&&c.method==='POST');assert.equal(first.body.revision,'m1');assert.equal(first.body.password_action,'keep');assert(!('password'in first.body));
  await p.getByLabel('Enable MQTT',{exact:true}).uncheck();await p.getByRole('button',{name:'Save MQTT settings',exact:true}).click();await p.waitForFunction(()=>document.querySelector('#mqttResult').textContent.includes('MQTT is disabled.'));
  const calls=s.calls.filter(c=>c.path==='/api/settings/mqtt'&&c.method==='POST');assert.equal(calls.length,2);assert.equal(calls[1].body.revision,'m2');assert.equal(calls[1].body.enabled,false);
 });
 await scenario('mqtt-lost-save-blocks-repeat-until-refresh',async(p,s)=>{
  s.handlers['POST /api/settings/mqtt']=({route})=>route.abort('failed');await p.getByRole('button',{name:'Save MQTT settings',exact:true}).click();await p.getByText(/The save response was lost/).waitFor();assert(await p.getByRole('button',{name:'Save MQTT settings',exact:true}).isDisabled());
  await p.evaluate(()=>render());assert.equal(s.calls.filter(c=>c.path==='/api/settings/mqtt'&&c.method==='POST').length,1);s.mqtt.revision='m2';await p.getByRole('button',{name:'Refresh saved settings',exact:true}).click();await p.getByText(/unsaved fields are preserved/).waitFor();assert(await p.getByRole('button',{name:'Save MQTT settings',exact:true}).isEnabled());
 });
 await scenario('mqtt-stale-refresh-preserves-edits',async(p,s)=>{
  s.handlers['POST /api/settings/mqtt']=({send})=>send({error:'settings_changed'},409);await p.getByLabel('Broker address',{exact:true}).fill('reviewed.example.invalid');await p.getByRole('button',{name:'Save MQTT settings',exact:true}).click();await p.getByText(/settings or bridge state changed/).waitFor();s.mqtt={...s.mqtt,revision:'m3',host:'concurrent.example.invalid'};
  await p.getByRole('button',{name:'Refresh saved settings',exact:true}).click();await p.getByText(/unsaved fields are preserved/).waitFor();assert.equal(await p.locator('#mqttHost').inputValue(),'reviewed.example.invalid');assert.equal(s.calls.filter(c=>c.method==='POST').length,1);
 });
 await scenario('mqtt-unconfirmed-durability-needs-review',async(p,s)=>{
  s.handlers['POST /api/settings/mqtt']=({send})=>send({saved:true,applied:true,durability_confirmed:false,settings:{...s.mqtt,revision:'m4'}});await p.getByRole('button',{name:'Save MQTT settings',exact:true}).click();await p.getByText(/saving them durably could not be confirmed/).waitFor();assert(await p.getByRole('button',{name:'Save MQTT settings',exact:true}).isDisabled());assert.equal(s.calls.filter(c=>c.path==='/api/settings/mqtt'&&c.method==='POST').length,1);
 });
 await scenario('cloud-preview-then-explicit-single-apply',async(p,s)=>{
  await preview(p);assert.equal(await p.locator('#cloudEmail').inputValue(),'');assert.equal(await p.locator('#cloudPassword').inputValue(),'');assert.equal(s.calls.filter(c=>c.path==='/api/cloud-import/apply').length,0);assert(await p.getByText('Hall lamp · #40002',{exact:true}).isVisible());
  await p.screenshot({path:path.join(evidence,'cloud-reviewed-import.png'),fullPage:true});await p.getByRole('button',{name:'Import reviewed metadata',exact:true}).click();await p.getByText(/Reviewed metadata imported/).waitFor();assert.deepEqual(s.calls.find(c=>c.path==='/api/cloud-import/apply').body,{preview_id:'p1',revision:'i1'});await p.evaluate(()=>render());assert.equal(s.calls.filter(c=>c.path==='/api/cloud-import/apply').length,1);
 });
 await scenario('cloud-different-mesh-and-conflict-refuse-apply',async(p,s)=>{
  s.handlers['POST /api/cloud-import/preview']=({send})=>send({...ready(),status:'blocked',can_apply:false,preview_id:null,selected_location:{id:'old',name:'Old mesh',matching_mesh:false},message:'A separate mesh takeover is needed.',changes:{devices_added:[],groups_added:[],members_added:[]}});await preview(p);assert(await p.getByRole('button',{name:'Import reviewed metadata',exact:true}).isDisabled());assert(await p.getByText(/uses a different mesh/).isVisible());assert.equal(s.calls.filter(c=>c.path==='/api/cloud-import/apply').length,0);
  await p.getByRole('button',{name:'Discard preview',exact:true}).click();s.handlers['POST /api/cloud-import/preview']=({send})=>send({...ready(),status:'blocked',can_apply:false,preview_id:null,message:'A cloud group conflicts with a protected local target.'});await preview(p);assert(await p.getByRole('button',{name:'Import reviewed metadata',exact:true}).isDisabled());assert(await p.getByText('A cloud group conflicts with a protected local target.',{exact:true}).first().isVisible());
 });
 await scenario('cloud-location-selection-clears-credentials',async(p,s)=>{
  s.handlers['POST /api/cloud-import/preview']=({body,send})=>body.location_id?send(ready()):send({status:'select_location',locations:[{id:'house',name:'Home',matching_mesh:true},{id:'cabin',name:'Cabin',matching_mesh:false}],selected_location:null,can_apply:false,preview_id:null,revision:'i1',changes:{},message:'Select a location.'});
  await credentials(p);await p.getByRole('button',{name:'Preview cloud import',exact:true}).click();await p.getByText(/Choose a location, then re-enter/).waitFor();assert.equal(await p.locator('#cloudPassword').inputValue(),'');await p.getByLabel('Location',{exact:true}).selectOption('house');await preview(p);const calls=s.calls.filter(c=>c.path==='/api/cloud-import/preview');assert.equal(calls.length,2);assert(!('location_id'in calls[0].body));assert.equal(calls[1].body.location_id,'house');
 });
 await scenario('cloud-expired-preview-and-lost-apply-never-retry',async(p,s)=>{
  s.handlers['POST /api/cloud-import/preview']=({send})=>send({...ready(),expires_in:0.05});await preview(p);await p.getByText(/This preview expired/).waitFor();assert(await p.getByRole('button',{name:'Import reviewed metadata',exact:true}).isDisabled());assert.equal(s.calls.filter(c=>c.path==='/api/cloud-import/apply').length,0);
  delete s.handlers['POST /api/cloud-import/preview'];await preview(p);s.handlers['POST /api/cloud-import/apply']=({route})=>route.abort('failed');await p.getByRole('button',{name:'Import reviewed metadata',exact:true}).click();await p.getByText(/import response was not confirmed/).waitFor();await p.evaluate(()=>render());assert.equal(s.calls.filter(c=>c.path==='/api/cloud-import/apply').length,1);assert.equal(await p.getByRole('button',{name:'Import reviewed metadata',exact:true}).count(),0);
 });
 await scenario('settings-mobile-and-cloud-name-escaping',async(p,s)=>{
  s.handlers['POST /api/cloud-import/preview']=({send})=>{const data=ready();data.selected_location.name='<img src=x onerror=alert(1)>';data.changes.devices_added[0].name='<script>window.injected=true</script>';return send(data);};await preview(p);assert.equal(await p.evaluate(()=>window.injected),undefined);assert.equal(await p.locator('#cloudReview script, #cloudReview img').count(),0);assert(await p.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));await p.screenshot({path:path.join(evidence,'settings-mobile.png'),fullPage:true});
 },true);
 for (const mobile of [false,true]) await scenario('relay selection and manual scan '+(mobile?'mobile':'desktop'),async(p,s)=>{
  const route='/api/settings/relay', first='02:00:00:00:00:01', other='02:00:00:00:00:02';
  let state={revision:'r1',configurable:true,preferred:null,current:first,scanned_at:1700000000,
    controllers:[{address:'02:00:00:00:00:03',name:'Hall keypad',device_id:40003,kind:'controller',rssi:-70,seen_at:1700000000,latest:true}],
    candidates:[{address:first,name:'Theatre 1',device_id:40001,identity:'pairing',rssi:-60,seen_at:1700000000,latest:true},
                {address:other,name:'Hall lamp',device_id:40002,identity:'observed',rssi:-65,seen_at:1700000000,latest:true}]};
  let scans=0;
  s.handlers['GET '+route]=({send})=>send(state);
  s.handlers['POST '+route+'/scan']=({body,send})=>{
    if (body.seconds === 60) {state.extended_scan={phase:'running',remaining_seconds:60};return send(state,202);}
    scans++;assert.deepEqual(body,{});
    state.candidates[0].latest=false;state.candidates[1].rssi=-49;
    return send(state);
  };
  s.handlers['POST '+route]=({body,send})=>{
    assert.equal(body.revision,state.revision);state={...state,revision:state.revision+'x',preferred:body.preferred?.address||null};
    return send(state);
  };
  s.handlers['POST '+route+'/reconnect']=({body,send})=>{
    assert.deepEqual(body,{revision:state.revision,current:first});state.current=null;return send(state,202);
  };
  await p.locator('#relayPanel summary').click();
  await p.getByText('Current relay: Theatre 1',{exact:true}).waitFor();
  // A light identified only while it was the connected relay says so; a paired light does not.
  assert(await p.locator('#relayList li').filter({hasText:'Hall lamp'}).getByText('Identified while connected as the relay').isVisible());
  assert.equal(await p.locator('#relayList li').filter({hasText:'Theatre 1'}).getByText('Identified while connected as the relay').count(),0);
  await p.getByLabel('Relay selection').selectOption('preferred');
  await p.getByLabel('Preferred light').selectOption(other);
  await p.getByRole('button',{name:'Scan now',exact:true}).click();
  await p.getByText(/-49 dBm/).waitFor();
  assert.equal(scans,1);
  assert(await p.getByText(/Not seen in latest scan/).isVisible());
  assert.equal(await p.getByLabel('Preferred light').inputValue(),other);
  assert.equal(state.current,first);
  assert(await p.locator('#relayControllers').getByText('Hall keypad').isVisible());
  assert.equal(await p.getByLabel('Preferred light').locator('option').filter({hasText:'Hall keypad'}).count(),0);
  await p.getByRole('button',{name:'Extended scan (60 seconds)'}).click();
  await p.getByText(/Extended scan in progress/).waitFor();
  assert(await p.getByRole('button',{name:'Scan now',exact:true}).isDisabled());
  assert.equal(await p.getByLabel('Preferred light').inputValue(),other);
  state.extended_scan={phase:'complete',remaining_seconds:0};
  state.candidates[0].rssi=null;
  await p.evaluate(()=>{S.devices=[{avion_id:40001,power_fresh:true},{avion_id:40002,power_fresh:true}];S.status.mesh_ready=true;
    S.status.relay_extended_scan={phase:'complete',remaining_seconds:0};render();});
  await p.getByText('Extended scan complete.').waitFor();
  // The connected relay reports over the connection itself; every other light reports through the mesh.
  const lightRow=p.locator('#relayList li').filter({hasText:'Theatre 1'});
  assert.match(await lightRow.textContent(),/Direct signal: No signal reading yet.*Mesh: Reporting directly \(connected relay\)/);
  assert.match(await p.locator('#relayList li').filter({hasText:'Hall lamp'}).textContent(),/Mesh: Reporting through mesh/);
  await p.getByRole('button',{name:'Save preference'}).click();
  await p.getByText('Preference saved for the next connection.').waitFor();
  assert.equal(state.preferred,other);
  await p.getByRole('button',{name:'Reconnect now'}).click();
  await p.getByText('Bluetooth relay disconnected').waitFor();
  state.current=first;state.fallback=true;
  await p.evaluate(()=>{S.status.relay_scan_number=2;render();});
  await p.getByText('Current relay: Theatre 1 (automatic fallback)').waitFor();
  await p.getByLabel('Relay selection').selectOption('automatic');
  await p.getByRole('button',{name:'Save preference'}).click();
  await p.getByText('Preference saved for the next connection.').waitFor();
  assert.equal(state.preferred,null);
  assert(await p.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
 },mobile);
 for (const mobile of [false,true]) await scenario('bridge update confirmation and recovery '+(mobile?'mobile':'desktop'),async(p,s)=>{
  const path='/api/settings/update', installed='0.5.0.dev81', latest='0.5.0.dev82';
  let state={installed,latest:null,job:{phase:'unavailable'},available:false}, writes=0;
  s.handlers['GET '+path]=({send})=>send(state);
  s.handlers['POST '+path+'/check']=({body,send})=>{
   assert.deepEqual(body,{});state={installed,latest,available:true,job:{phase:'idle'}};return send(state);
  };
  s.handlers['POST '+path+'/install']=({body,send})=>{
   assert.deepEqual(body,{version:latest,confirm_restart:true});writes++;
   state.job={phase:'preparing',message:'Downloading and verifying the release.'};return send(state,202);
  };
  await p.locator('#bridgeUpdatePanel summary').click();
  await p.locator('#updateSetup').waitFor();
  await p.getByRole('button',{name:'Check for updates'}).click();
  const offer=p.getByRole('button',{name:'Update to '+latest});
  await offer.click();await p.locator('#updateCancel').click();assert.equal(writes,0);
  await offer.click();await p.getByRole('button',{name:'Install and restart'}).click();
  await p.waitForFunction(()=>document.querySelector('#updateResult').textContent.includes('Downloading'));
  assert.equal(writes,1);
  state.job={phase:'rolled_back',message:'The previous bridge version is running again.'};
  await p.waitForFunction(()=>document.querySelector('#updateResult').textContent.includes('previous bridge version'),null,{timeout:8000});
  assert.equal(writes,1);assert(await p.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
 },mobile);
 await browser.close();fs.writeFileSync(path.join(evidence,'browser.json'),JSON.stringify({results,errors},null,2)+'\n');console.log(JSON.stringify({passed:results.filter(r=>r.passed).length,total:results.length,errors}));if(results.some(r=>!r.passed)||errors.length){console.log(JSON.stringify(results.filter(r=>!r.passed),null,2));process.exitCode=1;}
})().catch(e=>{console.error(e);process.exitCode=1;});
