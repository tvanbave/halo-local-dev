// Recently observed exact identities remain selectable; every pairing still calls the guarded backend once.
const assert=require('assert'),fs=require('fs'),path=require('path'),vm=require('vm');
const ui=fs.readFileSync(path.join(__dirname,'../avionmesh_pi/web/ui.js'),'utf8');
const source=ui.slice(ui.indexOf('const STEPS ='),ui.indexOf('/* ---------- activity & settings'));
const one={uuid:'1122334455661400000a000000000001',uuid_hash:123,identity:{vendor:20,product:10,candidate_model:134}};
const two={uuid:'2233445566771400000a000000000001',uuid_hash:124,identity:{vendor:20,product:10,candidate_model:134}};
function fixture(replies=[{devices:[one]},{devices:[]},{devices:[]}]){
 let now=100000;const calls=[],messages=[],S={status:{instance:'bridge-one',diagnostics:{session:1},mesh_ready:true,pending_transactions:0},caps:{features:{pairable_types:[93,134,127]}},devices:[]};
 class Clock extends Date {static now(){return now;}}
 const ctx=vm.createContext({S,Date:Clock,Map,Set,Number,Array,Object,JSON,I:{alert:''},TYPE_NAMES:{93:'RL4',134:'A19',127:'Keypad'},
  esc:v=>String(v??''),article:v=>v,feat:k=>k==='pair_lights',render:()=>{},renderSoon:()=>{},log:()=>{},toast:m=>messages.push(m),
  radio:async(label,fn)=>fn(()=>{}),sleep:async ms=>{now+=ms;},managementFresh:()=>true,refreshSnapshot:async()=>{},
  api:async(url,body)=>{calls.push({url,body});if(url==='/api/discover'){const next=replies.shift()||{devices:[]};if(next instanceof Error)throw next;return typeof next==='function'?next(S):next;}if(url==='/api/claim')return {associated:true,device_id:40001+calls.filter(c=>c.url==='/api/claim').length};throw Error(url);},
  modal:html=>messages.push(html),closeModal:()=>{}});
 vm.runInContext(source,ctx);const run=code=>vm.runInContext(code,ctx);run('S.wiz=newWizard()');
 return {S,calls,messages,run,setNow:n=>now=n,advance:ms=>now+=ms,replies};
}
(async()=>{
 let f=fixture();await f.run('startSearch()');
 assert.equal(f.run('wizRows().length'),1);assert.equal(f.run('rowReady(wizRows()[0])'),true);
 assert.equal(f.run('wizRows()[0].lastRound'),1);assert.equal(f.S.wiz.lastRound,3);
 f.advance(45000);assert.equal(f.run('rowSeenState(wizRows()[0])'),'recent');
 assert(f.run('foundRow(wizRows()[0])').includes('Seen recently — checked again before pairing'));
 assert(f.run('viewAdd()').includes('0 seen in the last 30 seconds. 1 seen recently'));
 await f.run('startClaims()');assert.equal(f.calls.filter(c=>c.url==='/api/claim').length,1);
 assert.equal(f.calls.find(c=>c.url==='/api/claim').body.uuid,one.uuid);
 console.log('PASS earlier-round identity remains selectable and pairs once after a later scan misses it');
 f=fixture([{devices:[one],candidates:[{...one,age_ms:1000,fresh:true}]},{devices:[],candidates:[{...one,age_ms:99999,fresh:false}]},{devices:[],candidates:[{...one,age_ms:99999,fresh:false}]}]);
 await f.run('startSearch()');assert.equal(f.run('wizRows()[0].lastSeen'),99000);
 f.setNow(399001);assert.equal(f.run('rowReady(wizRows()[0])'),false);
 await f.run('startSearch(true)');assert.equal(f.run('wizRows()[0].lastSeen'),99000);
 assert.equal(f.run('rowReady(wizRows()[0])'),false);await f.run('startClaims()');assert.equal(f.S.wiz.step,1);
 assert.equal(f.calls.filter(c=>c.url==='/api/claim').length,0);
 assert(f.run('viewAdd()').includes('1 older result; search again'));
 console.log('PASS history and new empty searches never extend last observation or admit an expired result');
 f=fixture();await f.run('startSearch()');f.setNow(99000);assert.equal(f.run('rowReady(wizRows()[0])'),false);
 f.setNow(110000);f.S.status.diagnostics.session=2;assert.equal(f.run('rowReady(wizRows()[0])'),false);
 f.S.status.diagnostics.session=1;f.S.status.instance='bridge-two';assert.equal(f.run('rowReady(wizRows()[0])'),false);
 console.log('PASS backwards clocks, reconnect sessions and bridge restarts invalidate the old selection');
 f=fixture([{devices:[one]},{devices:[{...two,uuid_hash:one.uuid_hash}]},{devices:[one]}]);await f.run('startSearch()');
 assert.equal(f.run('wizRows()[0].uuid'),one.uuid);assert(f.run('wizRows()[0].ambiguous'));assert(!f.run('rowReady(wizRows()[0])'));
 f=fixture([{devices:[one]},{devices:[],candidates:[{...one,ambiguous_hash:true,fresh:false}]},{devices:[]}]);await f.run('startSearch()');assert(!f.run('rowReady(wizRows()[0])'));
 console.log('PASS colliding UUIDs and persisted hash ambiguity cannot replace or enable the selected identity');
 f=fixture([{devices:[one]},{devices:[]},{devices:[],truncated:true}]);await f.run('startSearch()');assert(!f.run('rowReady(wizRows()[0])'));
 f.replies.push({devices:[]},{devices:[]},{devices:[]});await f.run('startSearch(true)');assert(f.run('rowReady(wizRows()[0])'));
 f=fixture([{devices:[one]},new Error('Discovery disconnected')]);await f.run('startSearch()');assert(!f.run('rowReady(wizRows()[0])'));
 console.log('PASS incomplete and failed searches disable pairing until a complete search finishes');
 f=fixture([state=>{state.status.instance='bridge-two';return {devices:[one]};}]);await f.run('startSearch()');assert.equal(f.run('wizRows().length'),0);assert(f.S.wiz.error.includes('connection changed'));
 f=fixture([{devices:[{...one,uuid:'not-a-uuid'},{...two,uuid_hash:undefined},{...one,identity:{candidate_model:999}}]},{devices:[]},{devices:[]}]);await f.run('startSearch()');assert.equal(f.run('wizRows().length'),1);assert(!f.run('rowReady(wizRows()[0])'));
 console.log('PASS old-process responses, invalid identities and unsupported models never become pairing requests');
 f=fixture([{devices:[one,two]},{devices:[]},{devices:[]}]);await f.run('startSearch()');
 f.run("refreshSnapshot=async()=>{S.status.diagnostics.session++;}");await f.run('startClaims()');
 assert.equal(f.calls.filter(c=>c.url==='/api/claim').length,2);
 f=fixture([{devices:[one,two]},{devices:[]},{devices:[]}]);await f.run('startSearch()');
 f.run("refreshSnapshot=async()=>{S.status.instance='restarted';}");await f.run('startClaims()');
 assert.equal(f.calls.filter(c=>c.url==='/api/claim').length,1);assert.equal(f.S.wiz.claims[1].st,'skip');
 console.log('PASS serial pairing survives its own session refresh but stops remaining devices after restart');
})().catch(e=>{console.error(e);process.exit(1);});
