// Synthetic VM tests of shipped recovery rendering; no bridge access.
const assert=require('assert'),fs=require('fs'),vm=require('vm'),path=require('path');
const root=path.join(__dirname,'../avionmesh_pi/web');
const ui=fs.readFileSync(path.join(root,'ui.js'),'utf8'),ops=fs.readFileSync(path.join(root,'ops.js'),'utf8');
const phaseFunction=ui.slice(ui.indexOf('function batteryPhaseText('),ui.indexOf('function batteryDetails('));
const t={id:'partial-one',target:40001,kind:'keypad_setup',status:'uncertain',detail:{product_id:126},recovery:{actions:['resolve_keypad_setup','reconcile'],reason:'partial_keypad_setup_read_or_continue'}};
const result={partial_resolution:true,resolved:true,applied_slots:[1],unchanged_attempted_slots:[2],unattempted_slots:[3,4],assignment_read_back:true,write_retried:false,setup_automatically_resumed:false,exposure_changed:false,reservations_retained:true};
function fixture(){
 const S={status:{instance:'one',jobs:{}},radio:{queue:[]}},posts=[],dialogs=[];
 const ctx=vm.createContext({S,Map,Set,Number,Object,Array,Date,JSON,esc:s=>String(s??''),I:{info:''},dev:id=>({name:'Battery keypad',product_type:126}),
  BATTERY_PHASES:{waiting_for_wake:'Press the bottom Power/Off button once now',reading:'Reading the keypad'},controllerReads:new Map(),keypadSaves:new Map(),batterySetups:new Map(),
  managementFresh:()=>true,renderSoon:()=>{},refreshSnapshot:async()=>{},setTimeout:()=>{},confirm:text=>{dialogs.push(text);return true;},
  api:async(url,body)=>{if(url==='/api/transactions')return {transactions:[t]};posts.push({url,body});return {accepted:true,job_id:'job-one'};}});
 vm.runInContext(phaseFunction+ops,ctx);const run=c=>vm.runInContext(c,ctx);ctx.t=structuredClone(t);ctx.result=structuredClone(result);run('Ops.list=[t]');return {S,run,ctx,posts,dialogs};
}
(async()=>{
 let f=fixture();await f.run("runOperation(t.id,'resolve_keypad_setup')");
 assert.deepEqual(f.posts,[{url:'/api/keypad/resolve-setup',body:{transaction:t.id}}]);
 assert(/two complete button readings/.test(f.dialogs[0])&&/no button changes/.test(f.dialogs[0])&&/Wait for each wake prompt/.test(f.dialogs[0]));
 assert.equal(f.run('Ops.results.get(t.id).action'),'resolve_keypad_setup');
 f.S.status.jobs[40001]={id:'job-one',state:'completed',result};f.run('settleOperationJobs()');
 assert.match(f.run('Ops.results.get(t.id).text'),/Closed; verified assignments kept/);
 assert.match(f.run('Ops.results.get(t.id).text'),/Applied buttons: 1/);
 assert.equal(f.run('Ops.results.get(t.id).review'),false);
 console.log('PASS explicit read-only action retains identity and exact partial outcome');
 f=fixture();assert.match(f.run('opsCard(t)'),/Continue setup \(may write\)/);
 f.ctx.t.status='cancelled';f.ctx.t.detail.partial_resolution={schema:'keypad-partial-setup-v1'};
 assert.match(f.run('opsCard(t)'),/Closed; verified assignments kept/);assert(!f.run('opsCard(t)').includes('Closed without a change'));
 for(const field of ['assignment_read_back','write_retried','setup_automatically_resumed','exposure_changed','reservations_retained']){
  f.ctx.result={...result};delete f.ctx.result[field];assert.equal(f.run('opsOutcome(t,OPS_ACTIONS.resolve_keypad_setup,result).review'),true);
 }
 console.log('PASS partial history and incomplete safety contract never claim no change or success');
 f=fixture();const p={transaction:t.id,target:40001,product_id:126,job_id:'job-one',instance:'one',operation:'keypad_recovery',phase:'waiting_for_wake'};
 f.S.status.jobs[40001]={id:'job-one',state:'running',progress:p};
 assert.match(f.run('opsCard(t)'),/Press the bottom Power\/Off/);assert.match(f.run('opsCard(t)'),/disabled/);
 await f.run("runOperation(t.id,'resolve_keypad_setup')");assert.equal(f.posts.length,0);
 for(const [key,value] of Object.entries({transaction:'other',target:40002,product_id:127,job_id:'other',instance:'old',operation:'keypad_save'})){
  f.S.status.jobs[40001].progress={...p,[key]:value};assert.equal(f.run('opsKeypadRecoveryProgress(t)'),'',key);
 }
 f.S.status.jobs[40001].progress=p;assert.match(f.run('batteryPhaseText(40001)'),/Power\/Off/);
 f.S.status.jobs[40001].state='completed';assert.equal(f.run('opsKeypadRecoveryProgress(t)'),'');
 console.log('PASS observer wake prompts require exact receipt job session and operation');
})().catch(e=>{console.error(e);process.exit(1)});
