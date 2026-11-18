/* Synthetic DOM contract checks; real browser coverage lives in test_settings_browser.cjs. */
const fs=require('fs'), path=require('path'), vm=require('vm'), assert=require('assert');
const source=fs.readFileSync(path.join(__dirname,'../avionmesh_pi/web/settings.js'),'utf8');
const clone=x=>JSON.parse(JSON.stringify(x));
const saved={revision:'r1',enabled:false,host:'broker.example.invalid',port:8883,username:'owner',tls:true,password_set:true,connected:false,source:'settings'};
function fixture() {
  const elements={}, calls=[], responses=[];
  class Element {
    constructor(id=''){this.id=id;this.value='';this.checked=false;this.disabled=false;this.required=false;this.hidden=false;this.textContent='';this.innerHTML='';this.parentNode=null;}
    querySelector(selector){const id=selector.startsWith('#')?selector.slice(1):selector;return elements[id]||(elements[id]=new Element(id));}
    replaceChildren(...children){this.children=children;for(const child of children)child.parentNode=this;}
    reportValidity(){return Object.values(elements).every(e=>!e.required||e.disabled||e.value!=='');}
  }
  for(const match of source.matchAll(/id="([A-Za-z0-9]+)"/g)) elements[match[1]]=new Element(match[1]);
  const mount=new Element('bridgeSettingsMount');let active=true;
  const document={createElement:()=>new Element(),getElementById:id=>id==='bridgeSettingsMount'?(active?mount:null):elements[id]};
  const ctx=vm.createContext({document,window:{confirm:()=>true},Date,console,setTimeout:()=>1,clearTimeout:()=>{},api:async(route,data)=>{
    calls.push({route,data:data===undefined?undefined:clone(data)});
    const next=responses.length?responses.shift():clone(saved);
    if(next instanceof Error)throw next;
    return next;
  }});
  vm.runInContext(source,ctx);const ui=ctx.window.BridgeSettings;
  const flush=async()=>{for(let i=0;i<6;i++)await Promise.resolve();};
  return {ui,e:elements,calls,responses,mount,flush,leave:()=>{active=false;ui.activate();},enter:()=>{active=true;ui.activate();}};
}
(async()=>{
 let n=0;const ok=name=>{n++;console.log('PASS '+name);};
 let f=fixture();assert(f.ui.html().includes('bridgeSettingsMount'));f.ui.activate();await f.flush();
 assert.equal(f.calls.length,1);assert.equal(f.calls[0].route,'/api/settings/mqtt');assert.equal(f.e.mqttPassword.value,'');assert.equal(f.e.mqttPasswordAction.value,'keep');
 ok('Initial GET populates public settings without a saved password');
 f.e.mqttHost.value='unsaved.example.invalid';f.e.mqttPasswordAction.value='replace';f.e.mqttPasswordAction.onchange();f.e.mqttPassword.value='synthetic-edit';const original=f.mount.children[0];f.ui.activate();await f.flush();
 assert.equal(f.mount.children[0],original);assert.equal(f.e.mqttHost.value,'unsaved.example.invalid');assert.equal(f.e.mqttPassword.value,'synthetic-edit');assert.equal(f.calls.length,1);
 ok('Snapshot rerender preserves the active form without another fetch');
 f.leave();assert.equal(f.e.mqttPassword.value,'');f.enter();assert.equal(f.e.mqttHost.value,'unsaved.example.invalid');
 ok('Navigation clears secrets while retaining nonsecret edits');
 f.e.mqttPassword.value='synthetic-edit';f.responses.push({connected:true,published:false,subscribed:false,saved:false});f.e.mqttTest.onclick();await f.flush();
 assert.equal(f.calls.at(-1).route,'/api/settings/mqtt/test');assert.equal(f.calls.at(-1).data.enabled,true);assert.equal(f.calls.at(-1).data.password,'synthetic-edit');assert.equal(f.e.mqttPassword.value,'');assert(f.e.mqttResult.textContent.includes('Re-enter'));assert(f.e.mqttResult.textContent.includes('does not confirm Home Assistant'));
 ok('Connection test is explicit and clears a replacement password');
 f.e.mqttEnabled.checked=true;f.e.mqttPassword.value='synthetic-save';f.responses.push({saved:true,settings:{...saved,revision:'r2',enabled:true,password_set:true}});f.e.mqttForm.onsubmit({preventDefault(){}});await f.flush();
 assert.equal(f.calls.at(-1).route,'/api/settings/mqtt');assert.equal(f.e.mqttPassword.value,'');assert.equal(f.e.mqttPasswordAction.value,'keep');assert(f.e.mqttResult.textContent.includes('saved'));
 ok('Save adopts the returned public revision and never echoes a password');
 f.responses.push(new Error('synthetic transport failure'));f.e.mqttForm.onsubmit({preventDefault(){}});await f.flush();const count=f.calls.length;
 assert(f.e.mqttFields.disabled);f.e.mqttForm.onsubmit({preventDefault(){}});await f.flush();assert.equal(f.calls.length,count);assert(f.e.mqttResult.textContent.includes('Nothing is retried'));
 ok('Lost save response blocks another mutation until explicit refresh');
 f.responses.push({...saved,revision:'r3'});f.e.mqttRefresh.onclick();await f.flush();assert(!f.e.mqttFields.disabled);assert.equal(f.calls.at(-1).route,'/api/settings/mqtt');
 ok('Explicit refresh releases uncertain save without an automatic retry');
 f.e.cloudEmail.value='owner@example.invalid';f.e.cloudPassword.value='synthetic-cloud';f.responses.push({status:'select_location',locations:[{id:'one',name:'House',matching_mesh:true},{id:'two',name:'Cabin',matching_mesh:false}],selected_location:null,can_apply:false,message:'Select'});f.e.cloudForm.onsubmit({preventDefault(){}});await f.flush();
 assert.equal(f.e.cloudEmail.value,'');assert.equal(f.e.cloudPassword.value,'');assert(f.e.cloudResult.textContent.includes('re-enter'));assert(f.e.cloudLocation.innerHTML.includes('different mesh'));assert.equal(f.calls.at(-1).route,'/api/cloud-import/preview');
 ok('Location selection keeps no cloud credentials and performs no import');
 const preview={status:'ready',locations:[{id:'one',name:'House',matching_mesh:true}],selected_location:{id:'one',name:'House',matching_mesh:true},can_apply:true,preview_id:'p1',revision:'c1',expires_in:300,changes:{devices_added:[{device_id:40002,name:'<unsafe-name>',product_id:93,exposed:false}],groups_added:[],members_added:[]},message:'Ready'};
 f.e.cloudEmail.value='owner@example.invalid';f.e.cloudPassword.value='synthetic-cloud';f.e.cloudLocation.value='one';f.responses.push(preview);f.e.cloudForm.onsubmit({preventDefault(){}});await f.flush();
 assert.equal(f.calls.at(-1).data.location_id,'one');assert(f.e.cloudReview.innerHTML.includes('&lt;unsafe-name&gt;'));assert(!f.e.cloudReview.innerHTML.includes('<unsafe-name>'));assert(f.e.cloudReview.innerHTML.includes('Import reviewed metadata'));
 ok('Reviewed preview escapes cloud names and waits for a separate apply');
 f.responses.push({saved:true,devices:2,groups:0,revision:'c2'});f.e.cloudApply.onclick();await f.flush();assert.deepEqual(f.calls.at(-1),{route:'/api/cloud-import/apply',data:{preview_id:'p1',revision:'c1'}});assert(f.e.cloudResult.textContent.includes('imported'));const done=f.calls.length;f.e.cloudApply.onclick();await f.flush();assert.equal(f.calls.length,done);
 ok('Apply sends only the reviewed token once, without credentials');
 f.e.cloudEmail.value='owner@example.invalid';f.e.cloudPassword.value='synthetic-cloud';f.responses.push({...preview,status:'blocked',can_apply:false,preview_id:null,selected_location:{id:'two',name:'Other',matching_mesh:false},message:'Different mesh'});f.e.cloudForm.onsubmit({preventDefault(){}});await f.flush();const before=f.calls.length;f.e.cloudApply.onclick();await f.flush();assert.equal(f.calls.length,before);assert(f.e.cloudReview.innerHTML.includes('uses a different mesh'));
 ok('A different mesh is visible but cannot be applied');
 f=fixture();f.ui.activate();await f.flush();f.responses.push({saved:true,applied:true,durability_confirmed:false,settings:{...saved,revision:'m4'}});f.e.mqttForm.onsubmit({preventDefault(){}});await f.flush();assert(f.e.mqttFields.disabled);assert(f.e.mqttResult.textContent.includes('may not survive a restart'));assert(!f.e.mqttResult.textContent.includes('MQTT settings saved.'));
 ok('Unconfirmed durability is not reported as a saved configuration');
 f=fixture();f.ui.activate();await f.flush();const rejected=new Error('Re-enter or clear the password when changing the broker.');rejected.code='mqtt_password_reentry_required';rejected.status=400;f.responses.push(rejected);f.e.mqttForm.onsubmit({preventDefault(){}});await f.flush();assert.equal(f.e.mqttResult.textContent,rejected.message);
 ok('Known sanitized backend errors preserve actionable guidance');
 console.log(n+' Settings UI checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
