// Pairing Name room drafts and the shipped metadata save path, entirely synthetic.
const assert=require('assert'),fs=require('fs'),vm=require('vm'),path=require('path');
const ui=fs.readFileSync(path.join(__dirname,'../avionmesh_pi/web/ui.js'),'utf8');
const source=ui.slice(ui.indexOf('async function saveLabels('),ui.indexOf('const deviceNameDrafts'))+ui.slice(ui.indexOf("const STEPS ="),ui.indexOf('/* ---------- activity & settings'));
function fixture(fail=false){
 const S={devices:[{avion_id:40001,name:'New A19',room:'Theatre',exposed:true},{avion_id:40002,name:'Other',room:'Bar'}],roomMap:{40001:'Theatre',40002:'Bar'},revision:'r1',status:{},caps:{features:{pairable_types:[134]}}},calls=[],messages=[];
 const ctx=vm.createContext({S,Map,Set,Number,Array,Object,JSON,Date,I:{flash:''},TYPE_NAMES:{134:'A19'},esc:v=>String(v??''),dev:id=>S.devices.find(d=>d.avion_id===id),roomOf:d=>d.room||'',rooms:()=>['Bar','Theatre'],feat:()=>true,
  $:()=>({focus:()=>{}}),render:()=>{},renderSoon:()=>{},toast:m=>messages.push(m),go:v=>S.view=v,refreshSnapshot:async()=>{},
  api:async(url,body)=>{calls.push({url,body});if(fail)throw Error('Room save failed');return url==='/api/device-rooms'?{saved:true,device_rooms:{...S.roomMap,...body.rooms}}:{saved:true,revision:'r2'};}});
 vm.runInContext(source,ctx);const run=c=>vm.runInContext(c,ctx);run("S.wiz=newWizard();S.wiz.step=3;S.wiz.claims=[{id:40001,type:134,st:'done',name:'New A19'},{id:40002,type:134,st:'fail'}];wizardRoomDraft(40001)");return {S,calls,messages,run};
}
(async()=>{
 let f=fixture();assert.match(f.run('viewAdd()'),/New room…/);
 f.run("chooseWizardRoom(40001,'__new');wizardRoomDraft(40001).name='  Master   closet  ';addWizardRoom(40001)");
 assert.equal(f.calls.length,0);assert.equal(f.run('wizardRoomDraft(40001).room'),'Master closet');
 f.run("S.wiz.names[40001]='Closet A19'");await f.run('finishWizard()');
 assert.deepEqual(f.calls,[{url:'/api/device-rooms',body:{rooms:{40001:'Master closet'}}},{url:'/api/metadata',body:{operation:'update',revision:'r1',kind:'device',target:40001,name:'Closet A19',exposed:true}}]);
 assert.equal(f.S.view,'home');assert.equal(f.S.devices[0].room,'Master closet');assert.equal(f.S.devices[1].room,'Bar');
 console.log('PASS new room and name use existing save path once for successfully paired devices only');
 f=fixture();f.run("chooseWizardRoom(40001,'__new');wizardRoomDraft(40001).name='Cancelled';wizardRoomDraft(40001).editing=false");await f.run('finishWizard()');assert.equal(f.calls.length,0);assert.equal(f.S.devices[0].room,'Theatre');
 f=fixture();f.run("chooseWizardRoom(40001,'__new');addWizardRoom(40001)");await f.run('finishWizard()');assert.equal(f.calls.length,0);assert(f.S.wiz);assert.match(f.run('wizardRoomDraft(40001).error'),/1–40/);
 console.log('PASS cancel and invalid draft preserve existing room without network requests');
 f=fixture(true);f.run("chooseWizardRoom(40001,'__new');wizardRoomDraft(40001).name='Closet';addWizardRoom(40001);S.wiz.names[40001]='Closet A19'");await f.run('finishWizard()');
 assert.equal(f.calls.length,1);assert.equal(f.S.devices[0].room,'Theatre');assert.equal(f.run('wizardRoomDraft(40001).room'),'Closet');assert.equal(f.S.wiz.names[40001],'Closet A19');assert.equal(f.S.wiz.saving,false);
 console.log('PASS failed room save retains Name step and drafts without attempting name or pairing commands');
 f=fixture();f.run("chooseWizardRoom(40001,JSON.stringify('Bar'))");await f.run('finishWizard()');assert.deepEqual(f.calls,[{url:'/api/device-rooms',body:{rooms:{40001:'Bar'}}}]);
 console.log('PASS selecting existing room uses unchanged metadata endpoint');
})().catch(e=>{console.error(e);process.exit(1)});
