/* Synthetic naming, pairing request and final metadata-save checks; no radio. */
const assert=require('assert'),fs=require('fs'),vm=require('vm'),path=require('path');
const ui=fs.readFileSync(path.join(__dirname,'../avionmesh_pi/web/ui.js'),'utf8');
const source=ui.slice(ui.indexOf('async function saveLabels('),ui.indexOf('const deviceNameDrafts'))+ui.slice(ui.indexOf('const STEPS ='),ui.indexOf('/* ---------- native scenes'));
function fixture(existing=[]){
 const devices=Array.from({length:10},(_,i)=>({avion_id:40001+i,name:'New light '+i,room:'Theatre',exposed:false}));
 const S={devices:[...existing,...devices,{avion_id:40100,name:'New keypad',room:'Theatre'}],roomMap:{},revision:'r1',status:{},caps:{features:{pairable_types:[93]}}},calls=[],messages=[];
 const ctx=vm.createContext({S,Map,Set,Number,Array,Object,JSON,Date,I:{flash:''},TYPE_NAMES:{93:'RL4'},document:{activeElement:null},
  esc:v=>String(v??''),dev:id=>S.devices.find(d=>d.avion_id===id),roomOf:d=>d.room||'',rooms:()=>['Theatre','Bar'],feat:()=>true,
  managementFresh:()=>true,log:()=>{},radio:async(label,fn)=>fn(()=>{}),
  $:()=>null,render:()=>{},renderSoon:()=>{},toast:m=>messages.push(m),go:v=>S.view=v,refreshSnapshot:async()=>{},
  api:async(url,body)=>{calls.push({url,body});if(url==='/api/claim'){const id=41000+calls.filter(c=>c.url==='/api/claim').length;S.devices.push({avion_id:id,name:body.name,exposed:false});return {device_id:id};}return url==='/api/device-rooms'?{saved:true,device_rooms:body.rooms}:{saved:true,revision:'r2'};}});
 vm.runInContext(source,ctx);const run=s=>vm.runInContext(s,ctx);
 run("S.wiz=newWizard();S.wiz.room='Theatre';S.wiz.claims=S.devices.filter(d=>d.avion_id>=40001&&d.avion_id<=40010).map(d=>({id:d.avion_id,type:93,st:'done',name:d.name}));S.wiz.claims.push({id:40100,type:127,st:'done'},{id:40200,type:93,st:'fail'},{id:40201,type:93,st:'skip'});S.wiz.step=3");
 return {S,calls,messages,run,names:()=>JSON.parse(JSON.stringify(S.wiz.names))};
}
(async()=>{
 let f=fixture();assert.equal(f.run('wizardNaming().enabled'),false);f.run('toggleWizardNames(true)');
 assert.deepEqual(Object.values(f.names()),Array.from({length:10},(_,i)=>'Theatre '+(i+1)));assert(!f.names()[40100]);assert(!f.names()[40200]);assert.equal(f.calls.length,0);
 await f.run('finishWizard()');assert.equal(f.calls.length,10);assert(f.calls.every(c=>c.url==='/api/metadata'&&c.body.operation==='update'&&c.body.exposed===false));
 assert.equal(f.S.devices.find(d=>d.avion_id===40100).name,'New keypad');assert.equal(f.S.view,'home');
 console.log('PASS opt-in creates ten sequential names and Finish saves only paired light metadata');
 f=fixture([{avion_id:1,name:'THEATRE 2'},{avion_id:2,name:'Theatre 10'}]);f.run('toggleWizardNames(true)');assert.equal(f.names()[40001],'Theatre 11');assert.equal(f.names()[40010],'Theatre 20');
 f.run("changeWizardNaming('start','1')");assert.equal(f.names()[40001],'Theatre 1');assert.equal(f.names()[40002],'Theatre 3');assert.equal(new Set(Object.values(f.names()).map(n=>n.toLowerCase())).size,10);
 console.log('PASS default continues after existing numbers; custom start skips case-insensitive collisions');
 f=fixture();f.run("toggleWizardNames(true);editWizardName(40001,'Screen left');changeWizardNaming('prefix','Cinema');changeWizardNaming('start','5')");
 assert.equal(f.names()[40001],'Screen left');assert.equal(f.names()[40002],'Cinema 5');
 f.run('toggleWizardNames(false)');assert.deepEqual(f.names(),{40001:'Screen left'});f.run('toggleWizardNames(true)');assert.equal(f.names()[40002],'Cinema 5');assert.equal(f.names()[40001],'Screen left');
 f.run("editWizardName(40002,'');changeWizardNaming('prefix','Other')");assert.equal(f.names()[40002],'');
 console.log('PASS edits including cleared names survive re-numbering and toggling off restores only generated drafts');
 f=fixture();f.run("S.wiz.names[40001]='My name';toggleWizardNames(true);chooseWizardRoom(40002,JSON.stringify('Bar'))");assert.equal(f.names()[40001],'My name');assert.equal(f.names()[40002],'Bar 1');assert.equal(f.names()[40003],'Theatre 1');
 f.run("editWizardName(40001,'Theatre 2')");assert.equal(f.names()[40003],'Theatre 3');assert.equal(f.names()[40004],'Theatre 4');
 console.log('PASS custom names are preserved and other rooms use independent numbering');
 f=fixture();f.run("toggleWizardNames(true);changeWizardNaming('prefix','')");const before=f.names();await f.run('finishWizard()');assert.equal(f.calls.length,0);assert(f.S.wiz);assert.match(f.messages.at(-1),/prefix/);
 f.run("changeWizardNaming('prefix','Room');changeWizardNaming('start','1.5')");await f.run('finishWizard()');assert.equal(f.calls.length,0);assert.match(f.messages.at(-1),/starting number/);
 f.run("changeWizardNaming('start','');changeWizardNaming('prefix','X'.repeat(71))");assert(f.run('wizardNaming().error'));assert.deepEqual(before[40001],'Theatre 1');
 console.log('PASS invalid prefixes and number drafts block Finish without changing devices');
 f=fixture();f.run("toggleWizardNames(true);S.devices.push({avion_id:7,name:'Theatre 1'})");await f.run('finishWizard()');assert.equal(f.calls.length,0);assert.match(f.messages.at(-1),/now in use/);
 f.run("changeWizardNaming('start','')");assert.equal(f.names()[40001],'Theatre 2');
 console.log('PASS a newly occupied name is caught before saving and requires a refreshed preview');
 f=fixture();f.run("S.wiz.step=0;S.wiz.room='';toggleWizardNames(true);S.wiz.claims=[{id:40100,type:127,st:'done'}];namePairedDevices()");assert.equal(f.run('wizardNaming().error'),'');await f.run('finishWizard()');assert.equal(f.calls.length,0);
 console.log('PASS an enabled naming option never blocks a controller-only batch');
 f=fixture([{avion_id:1,name:'Living Room 2'}]);
 f.run("S.wiz=newWizard();S.wiz.step=1;S.wiz.room='Living Room';S.wiz.searchComplete=true;S.status={instance:'one',mesh_ready:true,pending_transactions:0};wizardNaming().enabled=true;S.wiz.found={1:{uuid:'0011223344561400000a000000000001',uuid_hash:1,hash_hex:'00000001',detected_product_type:93,lastSeen:Date.now(),epoch:wizardDiscoveryEpoch(),sel:true},2:{uuid:'1021324354661400000a000000000002',uuid_hash:2,hash_hex:'00000002',detected_product_type:93,lastSeen:Date.now(),epoch:wizardDiscoveryEpoch(),sel:true}}");
 await f.run('startClaims()');
 assert.deepEqual(f.calls.filter(c=>c.url==='/api/claim').map(c=>c.body.name),['Living Room 3','Living Room 4']);
 assert.equal(f.S.devices.find(d=>d.avion_id===41001).name,'Living Room 3');assert.equal(f.S.wiz.step,2);
 f.run('namePairedDevices()');assert.deepEqual(f.names(),{41001:'Living Room 3',41002:'Living Room 4'});
 f.run('toggleWizardNames(false)');assert.equal(f.names()[41001],'Living Room 3');
 await f.run('finishWizard()');assert(!f.calls.some(c=>c.url==='/api/metadata'),'Already saved automatic names must not be renamed again');
 console.log('PASS opted-in names are saved with pairing, survive leaving before Finish, and remain editable');
 f=fixture();f.run("S.wiz=newWizard();wizardNaming().enabled=true;S.wiz.room='Theatre';wizardNaming().start='999999'");
 assert.throws(()=>f.run("wizardPairingNames([{uuid:'one',detected_product_type:93},{uuid:'two',detected_product_type:93}])"),/No number/);assert.equal(f.calls.length,0);
 f.run("wizardNaming().enabled=false");assert.equal(f.run("wizardPairingNames([{uuid:'one',detected_product_type:93}]).size"),0);
 console.log('PASS invalid batch numbering makes no requests and naming remains opt-in');
})().catch(e=>{console.error(e);process.exit(1)});
