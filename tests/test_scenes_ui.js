/* Scene form/action tests. Synthetic API only; no live mesh or broker. */
const assert=require('assert'),fs=require('fs'),vm=require('vm'),path=require('path');
const ui=fs.readFileSync(path.join(__dirname,'../avionmesh_pi/web/ui.js'),'utf8');
const source=ui.slice(ui.indexOf('/* ---------- native scenes ---------- */'),ui.indexOf('/* ---------- activity & settings ---------- */'));
function fixture(){
 const devices=[{avion_id:40001,name:'A19 <reading>',product_type:134,kind:'light',brightness:128,color_temp:4000},{avion_id:40002,name:'Downlight',product_type:93,kind:'light'},{avion_id:40003,name:'Keypad',product_type:127,kind:'controller'}];
 const state={scenes:[],operations:[],capabilities:{supported_models:[93,134]},job:null},calls=[],events=[];
 const S={devices,sceneWorkflow:state,status:{mesh_ready:true,pending_transactions:0},health:{mqtt_connected:true},drawer:null};
 const f={S,state,calls,events,fail:false};
 const ctx=vm.createContext({S,Map,Set,Object,JSON,Number,Array,String,I:{info:'',alert:'',plus:'',layers:'',close:''},console,
  esc:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),feat:()=>true,managementFresh:()=>true,
  lights:()=>devices.filter(d=>d.kind!=='controller'),dev:id=>devices.find(d=>d.avion_id===id),byName:(a,b)=>a.name.localeCompare(b.name),pct:b=>Math.round(b*100/255),raw:p=>Math.round(p*255/100),known:d=>d.brightness!==undefined,
  roomOf:()=>'',typeName:d=>d.product_type===134?'A19':'Light',lightingGroups:()=>[{group_id:300,name:'Room',members:[40001,40002]}],members:()=>devices.slice(0,2),validLabel:(s,n)=>typeof s==='string'&&!!s.trim()&&s.length<=n&&!/[\x00-\x1f\x7f]/.test(s),
  openDrawer:(kind,id)=>S.drawer={kind,id},closeDrawer:()=>{S.drawer=null;events.push('close')},render:()=>{},renderSoon:()=>{},toast:s=>events.push(s),log:()=>{},modal:html=>events.push(html),closeModal:()=>{},$:()=>null,
  getJson:async()=>state,api:async(url,body)=>{calls.push({url,body:JSON.parse(JSON.stringify(body))});if(f.fail)throw Error('Write outcome unknown');if(f.refuse)throw Object.assign(Error('The mesh is busy.'),f.refuse);return f.started===false?{saved:true}:{started:true,job:{id:f.jobId}};}});
 vm.runInContext(source,ctx);f.run=s=>vm.runInContext(s,ctx);return f;
}
(async()=>{
 let f=fixture();f.run("openScene();sceneChangeName('Evening');sceneChooseGroup(300);sceneMemberChange(40001,'percent','25');sceneMemberChange(40002,'kelvin','3500');sceneDraft.exposed=true");
 assert(!f.run('sceneDrawer()').includes('Keypad'));assert.match(f.run('sceneDrawer()'),/A19 &lt;reading&gt;/);await f.run('saveScene()');
 assert.deepStrictEqual(f.calls,[{url:'/api/scenes/create',body:{name:'Evening',members:[{target:40001,brightness:64,kelvin:4000},{target:40002,brightness:255,kelvin:3500}],exposed:true}}]);assert.equal(f.S.drawer,null);
 console.log('PASS one multi-light create request with explicit per-light values and sharing; controllers excluded');
 f=fixture();f.run("openScene();sceneChangeName('Evening');sceneToggleMember(40001,true);sceneMemberChange(40001,'percent','')");await f.run('saveScene()');assert.equal(f.calls.length,0);assert.match(f.run('sceneDraft.error'),/whole number/);
 f.run("sceneMemberChange(40001,'percent','100');sceneMemberChange(40001,'kelvin','9000')");await f.run('saveScene()');assert.equal(f.calls.length,0);
 console.log('PASS invalid values do not send scene writes');
 f=fixture();f.fail=true;f.run("openScene();sceneChangeName('Keep draft');sceneToggleMember(40001,true)");await f.run('saveScene()');assert.equal(f.calls.length,1);assert.equal(f.run('sceneDraft.name'),'Keep draft');assert(f.S.drawer);assert.match(f.run('sceneDraft.error'),/Check the saved operation/);
 f.run('renderSoon();renderSoon()');assert.equal(f.calls.length,1);
 console.log('PASS uncertain request retains draft and is not retried');
 f=fixture();f.state.scenes=[{id:'s1',name:'Stored',status:'ready',exposed:false,members:[{target:40001,brightness:128,kelvin:4000}]}];f.run("openScene('s1');sceneChangeName('Edited');sceneToggleMember(40002,true)");await f.run('saveScene()');assert.equal(f.calls[0].url,'/api/scenes/edit');assert.equal(f.calls[0].body.id,'s1');assert(!('exposed' in f.calls[0].body));
 f=fixture();f.state.scenes=[{id:'s1',name:'Stored',status:'ready',exposed:false,members:[{target:40001,brightness:128,kelvin:4000}]}];await f.run("shareScene('s1',true)");assert.equal(f.calls[0].url,'/api/scenes/exposure');assert.equal(f.calls[0].body.exposed,true);
 console.log('PASS editing and Home Assistant sharing use separate explicit actions');
 f=fixture();f.state.scenes=[{id:'s1',name:'Check first',status:'ready',activation_ready:false,members:[{target:40001,brightness:128,kelvin:4000}]}];
 await f.run("activateScene('s1')");assert.equal(f.calls.length,0);assert.match(f.run('viewScenes()'),/Check saved settings/);assert.match(f.run('viewScenes()'),/Verification does not turn the lights on/);
 await f.run("verifyScene('s1')");assert.equal(f.calls[0].url,'/api/scenes/verify');
 f=fixture();f.state.job={state:'running',operation:'activate'};assert.match(f.run('sceneProgressHtml()'),/Sending one command/);assert(!f.run('sceneProgressHtml()').includes('Larger installations take longer'));
 console.log('PASS scene activation never hides verification behind a button press');

 f=fixture();f.state.scenes=[{id:'s1',name:'Pending',status:'needs_review',members:[]}];f.state.operations=[{transaction:'txn',scene_id:'s1',state:'uncertain',progress:{phase:'verifying',target:40001}}];assert.equal(f.run('sceneCanWrite()'),false);await f.run("activateScene('s1')");assert.equal(f.calls.length,0);assert.match(f.run('viewScenes()'),/Read and recover/);await f.run("recoverScene('txn')");assert.equal(f.calls[0].url,'/api/scenes/recover');
 console.log('PASS unfinished scene blocks activation and offers read-only recovery');
 f=fixture();f.state.operations=[{transaction:'txn',state:'uncertain',progress:{recovery_ready:false}}];await f.run("resumeScene('txn')");assert.equal(f.calls.length,0);
 f.state.operations[0].progress={recovery_ready:true,retry_requires_explicit_action:true};assert.match(f.run('sceneProgressHtml()'),/Retry full preset and continue/);await f.run("resumeScene('txn')");assert.equal(f.calls.length,1);assert.equal(f.calls[0].url,'/api/scenes/resume');
 f=fixture();f.state.job={state:'complete',operation:'activate',result:{sent:true}};assert(!f.run('sceneProgressHtml()').includes('Check the lights'),'a result this page did not watch is not shown again');
 f.state.job={state:'running',operation:'recover'};f.run('sceneProgressHtml()');f.state.job={state:'complete',operation:'recover',result:{verified:true,desired_matched:false,outcome:'unchanged'}};assert.match(f.run('sceneProgressHtml()'),/requested full preset was not confirmed/);
 f.state.job={state:'running',operation:'activate'};f.run('sceneProgressHtml()');f.state.job={state:'complete',operation:'activate',result:{sent:true,physical_state_confirmed:false}};assert.match(f.run('sceneProgressHtml()'),/Check the lights/);
 console.log('PASS continuation requires verified recovery and unchanged readback is not presented as saved');
 f=fixture();f.state.operations=[{transaction:'pair',state:'uncertain',progress:{schema:2,recovery_ready:true,recovery_mode:'continue_unattempted',steps:[{target:40001,property:'preset',status:'attempted',attempts:[{commands:[{property:'colour',status:'transport_accepted'},{property:'brightness',status:'not_started'}]}]},{target:40002,property:'preset',status:'verified'}]}}];
 let html=f.run('sceneProgressHtml()');assert.match(html,/A19 &lt;reading&gt; · Full preset not yet confirmed/);assert.match(html,/Downlight · Full preset verified/);assert.match(html,/only the remaining part that was never attempted, then verifies both settings/);assert.match(html,/>Continue save<\/button>/);assert.match(html,/>Read and recover<\/button>/);assert(!/colour:|transport accepted/.test(html));assert.equal(f.calls.length,0);
 f.state.operations[0].progress.steps=[{target:40001,property:'colour',status:'verified'},{target:40001,property:'brightness',status:'not_started'}];html=f.run('sceneProgressHtml()');assert.equal((html.match(/<li>/g)||[]).length,1);assert.match(html,/Full preset not yet confirmed/);assert(!html.includes('Full preset verified'));
 console.log('PASS progress describes whole light presets and keeps reading separate from explicit continuation');
 f=fixture();f.state.operations=[{transaction:'closed',state:'verified',progress:{}}];assert.equal(f.run('sceneUnfinished().length'),0);
 f.state.operations.push({transaction:'legacy',state:'uncertain',progress:{legacy_ambiguous:true,legacy_sources:['closed'],recovery_ready:false,steps:[{target:40001,property:'colour',status:'verified'},{target:40001,property:'brightness',status:'verified'}]}});
 assert.equal(f.run('sceneUnfinished().length'),1);assert.equal(f.run('sceneCanWrite()'),false);html=f.run('sceneProgressHtml()');assert.match(html,/earlier save may have sent only part/i);assert(!html.includes('Full preset verified'));assert.match(html,/>Read and recover<\/button>/);assert(!html.includes('onclick="resumeScene'));await f.run("resumeScene('legacy')");assert.equal(f.calls.length,0);await f.run("recoverScene('legacy')");assert.deepStrictEqual(f.calls,[{url:'/api/scenes/recover',body:{transaction:'legacy'}}]);
 console.log('PASS ambiguous legacy receipt blocks changes until explicit read-only recovery without reopening verified receipts');
 f=fixture();f.state.scenes=[{id:'s1',name:'Saved',status:'ready',activation_ready:true,members:[{target:40001,brightness:128,kelvin:4000}]}];
 f.run("openScene('s1')");html=f.run('sceneDrawer()');assert.match(html,/Activation troubleshooting/);assert.match(html,/Test alternative activation/);
 await f.run("testSceneActivation('s1')");assert.deepStrictEqual(f.calls,[{url:'/api/scenes/activate-keypad-test',body:{id:'s1'}}]);
 f=fixture();f.state.scenes=[{id:'s1',name:'Saved',status:'ready',activation_ready:true,members:[{target:40001,brightness:128,kelvin:4000}]}];
 f.run("openScene('s1');sceneChangeName('Unsaved')");await f.run("testSceneActivation('s1')");assert.equal(f.calls.length,0);
 f.run("sceneDraft.dirty=false;sceneById('s1').activation_ready=false");await f.run("testSceneActivation('s1')");assert.equal(f.calls.length,0);
 f.run("sceneById('s1').activation_ready=true");await f.run("activateScene('s1')");assert.deepStrictEqual(f.calls,[{url:'/api/scenes/activate',body:{id:'s1'}}]);
 console.log('PASS alternative recall is explicit, requires a saved verified scene and leaves normal activation unchanged');
 f=fixture();f.state.scenes=[{id:'s1',name:'Saved',status:'ready',activation_ready:true,members:[{target:40001,brightness:128,kelvin:4000}]}];
 f.run("openScene('s1')");assert.match(f.run('sceneDrawer()'),/Test without status reads/);
 await f.run("testQuietSceneActivation('s1')");assert.deepStrictEqual(f.calls,[{url:'/api/scenes/activate-quiet-test',body:{id:'s1'}}]);
 f.run("sceneDraft.dirty=true");await f.run("testQuietSceneActivation('s1')");assert.equal(f.calls.length,1);
 f.run("sceneDraft.dirty=false;sceneById('s1').activation_ready=false");await f.run("testQuietSceneActivation('s1')");assert.equal(f.calls.length,1);
 f.state.job={state:'running',operation:'activate-quiet-test'};html=f.run('sceneProgressHtml()');assert.match(html,/Watch the lights now/);assert.match(html,/three seconds/);assert(!html.includes('Larger installations take longer'));
 console.log('PASS quiet comparison is one explicit normal-scene request with clear status delay wording');
 // ---- refusals, silent lights, set aside, forget, unsaved edits ----
 f=fixture();f.refuse={status:409,details:{operation_started:false}};f.run("openScene();sceneChangeName('Busy');sceneToggleMember(40001,true)");await f.run('saveScene()');
 assert.match(f.run('sceneDraft.error'),/^Not started\. /);assert(!f.run('sceneDraft.error').includes('Check the saved operation'));
 console.log('PASS a request refused before it started does not point at a saved operation');
 f=fixture();f.state.scenes=[{id:'s1',name:'Saved',status:'ready',activation_ready:false,members:[{target:40001,brightness:128,kelvin:4000}]}];
 f.jobId='j1';await f.run("verifyScene('s1')");f.state.job={id:'j1',state:'failed',operation:'verify',error:'Light 40002 did not answer. Nothing more was sent.',unreachable:40002};
 html=f.run('sceneProgressHtml()');assert.match(html,/Carry on without Downlight/);assert.match(html,/Nothing was changed/);
 await f.run('sceneCarryOnUnchecked()');assert.deepStrictEqual(f.calls[1],{url:'/api/scenes/verify',body:{id:'s1',skip:[40002]}});
 f=fixture();f.jobId='j2';f.run("openScene();sceneChangeName('New');sceneToggleMember(40001,true)");await f.run('saveScene()');
 f.state.job={id:'j2',state:'failed',operation:'create',error:'Light 40001 did not answer.',unreachable:40001};html=f.run('sceneProgressHtml()');
 assert.match(html,/This request changes .*so it must answer/);assert(!html.includes('Carry on without'));await f.run('sceneCarryOnUnchecked()');assert.equal(f.calls.length,1);
 f.state.job={id:'other',state:'failed',operation:'create',error:'Light 40002 did not answer.',unreachable:40002};assert(!f.run('sceneProgressHtml()').includes('Carry on without'),'only the request this page started can be repeated');
 f.state.job={id:'j2',state:'failed',operation:'create',error:'x',unreachable:40002};f.state.operations=[{transaction:'txn',scene_id:'s9',state:'uncertain',progress:{steps:[{target:40001,property:'preset',status:'attempted'}]}}];
 assert(!f.run('sceneProgressHtml()').includes('Carry on without'),'never offered once a saved operation exists');await f.run('sceneCarryOnUnchecked()');assert.equal(f.calls.length,1);
 console.log('PASS an unpowered light can be left unchecked only for a request that changed nothing');
 // An unchanged member may be skipped while another member changes. Adding,
 // removing, brightness changes and colour changes still require that light.
 for (const [change, maySkip] of [
   ["sceneMemberChange(40001,'percent','25')",true],
   ["sceneMemberChange(40002,'percent','25')",false],
   ["sceneMemberChange(40002,'kelvin','4000')",false],
   ["sceneToggleMember(40002,false)",false]
 ]) {
  f=fixture();f.jobId='edit-job';f.state.scenes=[{id:'s1',name:'Saved',status:'ready',activation_ready:true,members:[{target:40001,brightness:128,kelvin:4000},{target:40002,brightness:150,kelvin:3000}]}];
  f.run("openScene('s1');"+change);await f.run('saveScene()');f.state.job={id:'edit-job',state:'failed',operation:'edit',error:'Silent',unreachable:40002};
  html=f.run('sceneProgressHtml()');assert.equal(html.includes('Carry on without Downlight'),maySkip,change);
  if(maySkip){assert.match(html,/could respond when you activate this scene, even if you did not select it/);assert(html.indexOf('same scene slot')<html.indexOf('Carry on without Downlight'),'consequence is explained before the action');}
  await f.run('sceneCarryOnUnchecked()');assert.equal(f.calls.length,maySkip?2:1,change);
  if(maySkip)assert.deepStrictEqual(f.calls[1].body.skip,[40002]);
 }
 f=fixture();f.jobId='add-job';f.state.scenes=[{id:'s1',name:'Saved',status:'ready',activation_ready:true,members:[{target:40001,brightness:128,kelvin:4000}]}];
 f.run("openScene('s1');sceneToggleMember(40002,true)");await f.run('saveScene()');f.state.job={id:'add-job',state:'failed',operation:'edit',error:'Silent',unreachable:40002};
 assert(!f.run('sceneProgressHtml()').includes('Carry on without'));await f.run('sceneCarryOnUnchecked()');assert.equal(f.calls.length,1);
 console.log('PASS unchanged edit members can be skipped, with the activation consequence explained; changed, added and removed members cannot');
 f=fixture();f.state.operations=[{transaction:'txn',scene_id:'s1',state:'uncertain',progress:{action:'create',unreachable:40002,assumed:[40003],steps:[{target:40001,property:'preset',status:'verified'}]}}];
 html=f.run('sceneProgressHtml()');assert.match(html,/Read and recover without Downlight/);assert.match(html,/Set aside…/);
 assert.match(html,/could respond when you activate this scene, even if you did not select it/);assert(html.indexOf('same scene slot')<html.indexOf('Read and recover without Downlight'),'recovery explains the activation consequence before the action');
 await f.run("recoverSceneUnchecked('txn',40001)");assert.equal(f.calls.length,0,'a light this save writes must be read');
 await f.run("recoverSceneUnchecked('txn',40002)");assert.deepStrictEqual(f.calls,[{url:'/api/scenes/recover',body:{transaction:'txn',skip:[40003,40002]}}]);
 f.state.operations[0].progress.unreachable=40001;html=f.run('sceneProgressHtml()');assert.match(html,/is part of this save, so it must be read/);assert(!html.includes('Read and recover without'));
 console.log('PASS recovery can skip only a light the save never changes');
 f=fixture();f.state.operations=[{transaction:'txn',scene_id:'s1',state:'uncertain',progress:{action:'edit',prior:{name:'Old'},steps:[]}}];f.started=false;
 f.run("confirmSetAsideScene('txn')");assert.equal(f.calls.length,0,'nothing happens before the owner confirms');
 assert.match(f.events.at(-1),/sends nothing to the lights/);assert.match(f.events.at(-1),/stays in your list/);
 await f.run("setAsideScene('txn')");assert.deepStrictEqual(f.calls,[{url:'/api/scenes/set-aside',body:{transaction:'txn'}}]);assert(f.events.includes('Set aside. Nothing was sent to the lights.'));
 f.state.operations=[{transaction:'legacy',scene_id:'s1',state:'uncertain',progress:{legacy_ambiguous:true,legacy_sources:['a']}}];assert(!f.run('sceneProgressHtml()').includes('Set aside…'));
 console.log('PASS setting aside is explicit, explained and sends one bridge-only request');
 f=fixture();f.started=false;f.state.scenes=[{id:'s1',name:'Stuck',status:'needs_review',set_aside:true,unchecked:[40002],members:[{target:40001,brightness:128,kelvin:4000}]}];
 html=f.run('viewScenes()');assert.match(html,/A change to this scene was set aside/);assert.match(html,/Not checked on Downlight/);
 f.run("openScene('s1')");assert.match(f.run('sceneDrawer()'),/Forget scene…/);f.run("confirmForgetScene('s1')");assert.equal(f.calls.length,0);assert.match(f.events.at(-1),/Nothing is sent to the lights/);
 await f.run("forgetScene('s1')");assert.deepStrictEqual(f.calls,[{url:'/api/scenes/forget',body:{id:'s1'}}]);assert.equal(f.S.drawer,null);
 f=fixture();f.state.error='Saved scene records could not be read, so scenes are paused.';f.state.reserved_references=[3];html=f.run('viewScenes()');assert.match(html,/scenes are paused/);assert.match(html,/1 scene slot is reserved/);
 console.log('PASS forgetting a scene is explicit and bridge-only; paused scenes and reserved slots are explained');
 f=fixture();f.state.job={id:'old',state:'failed',operation:'verify',error:'Scene ownership or preset changed; activation blocked'};html=f.run('sceneProgressHtml()');
 assert.match(html,/activation blocked/);assert.match(html,/>Dismiss<\/button>/);f.run("sceneDismissed='old'");assert(!f.run('sceneProgressHtml()').includes('activation blocked'));
 console.log('PASS an old failure can be dismissed');
 // ---- a finished job stays finished, and its result is shown on the page that started it ----
 f=fixture();f.state.scenes=[{id:'s1',name:'Saved',status:'ready',activation_ready:true,members:[{target:40001,brightness:128,kelvin:4000}]}];
 assert.equal(f.run("sceneAccept({...S.sceneWorkflow,job:{id:'j9',operation:'activate',state:'complete',result:{sent:true}}})"),true);
 assert.equal(f.run("sceneAccept({...S.sceneWorkflow,job:{id:'j9',operation:'activate',state:'running'}})"),false,'a late reply must not bring a finished job back');
 assert.equal(f.run('S.sceneWorkflow.job.state'),'complete');assert.equal(f.run('sceneRunning()'),false);
 assert.equal(f.run("sceneAccept({...S.sceneWorkflow,job:{id:'j10',operation:'activate',state:'running'}})"),true,'a new job is accepted');
 assert.equal(f.run("sceneAccept({...S.sceneWorkflow,job:{operation:'activate',state:'running'}})"),true,'a job without an identifier is not filtered');
 console.log('PASS a late running reply cannot replace a job already seen finished');
 f=fixture();f.state.scenes=[{id:'s1',name:'Saved',status:'ready',activation_ready:true,members:[{target:40001,brightness:128,kelvin:4000}]}];f.jobId='j11';
 f.run("sceneAccept({...S.sceneWorkflow,job:{id:'j11',operation:'activate',state:'complete',result:{sent:true}}})"); // the live stream reported the end first
 f.state.job={id:'j11',operation:'activate',state:'complete',result:{sent:true}};await f.run("activateScene('s1')");
 assert.equal(f.run('S.sceneWorkflow.job.state'),'complete');html=f.run('sceneProgressHtml()');assert(!html.includes('Activating scene'));assert.match(html,/Scene activation sent\. Check the lights/);
 const before=f.run('sceneEpoch');await f.run('refreshScenes()');assert(f.run('sceneEpoch')>before,'a direct read marks the page for redraw on the next update');
 console.log('PASS the starting page shows the result even when the end arrives before the request reply');
})().catch(e=>{console.error(e);process.exitCode=1});
