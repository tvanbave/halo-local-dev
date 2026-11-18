// The shipped UI functions with synthetic fetch/DOM. No bridge is contacted.
const assert = require('assert'), fs = require('fs'), path = require('path'), vm = require('vm');
const source = fs.readFileSync(path.join(__dirname, '../avionmesh_pi/web/ui.js'), 'utf8');
const api = source.slice(source.indexOf('const ERRORS ='), source.indexOf('const getJson ='));
const groups = source.slice(source.indexOf('/* ---------- reviewed group management'), source.indexOf('/* ---------- modals'));

function fixture(result, status = 409) {
  const calls = [], notices = [], modals = [], button = {disabled:false}, group = {group_id:300,name:'Theatre',members:[],protected:false};
  const state = {synced:true, revision:'reviewed', radio:{label:null,queue:[]},
    status:{mesh_ready:true,pending_transactions:0,jobs:{},instance:'one'},
    groupWorkflow:{programming_enabled:true}};
  const context = vm.createContext({S:state, Map, Set, Date:{now:()=>1000},
    grp:()=>group, feat:()=>true, modal:html=>modals.push(html), closeDrawer:()=>notices.push('drawer closed'),
    dev:()=>({name:'Synthetic light'}), gName:()=> 'Theatre', $:()=>button,
    confirm:()=>true, opsReviewButton:(label='Review Operations')=>`<button class="btn" onclick="go('operations')">${label}</button>`, toast:m=>notices.push(m), esc:String,
    refreshSnapshot:async()=>{}, closeModal:()=>{}, openDrawer:()=>{}, renderSoon:()=>{},
    fetch:async(url, options)=>{
      calls.push({url,options});
      if (result instanceof Error) throw result;
      return {ok:status<400,status,json:async()=>result};
    }});
  vm.runInContext(api + groups, context);
  vm.runInContext("snapshotAt=1000;groupDraft={id:300,member:true,revision:'reviewed',instance:'one',selected:new Set([40002]),allowed:[40002]}", context);
  return {calls, context, group, notices, modals, state, run:code=>vm.runInContext(code,context)};
}

(async()=>{
  let passed = 0;
  for (const error of ['background_read_busy','mesh_busy','mesh_offline','session_changed','inventory_changed']) {
    const f = fixture({error,message:'Request was not started.',operation_started:false,requires_review:false,retry_allowed:false});
    await f.run('applyGroupMembers()');
    assert.equal(f.calls.length,1,error+' must not trigger HTTP retry');
    assert.equal(f.run('groupOutcomes.get(300).blocked'),false);
    assert.equal(f.run('groupWritable(300)'),true);
    assert(f.run('groupOutcomes.get(300).message').includes('Group change not started'));
    assert(!f.run('groupResultHtml(300)').includes('Review Operations'));
    passed++;
  }
  for (const result of [
    {error:'pending_operation',message:'An unfinished operation needs review.',operation_started:false,requires_review:true},
    {error:'operation_unavailable_or_uncertain'},
    {error:'mesh_busy'}, // A code alone is not proof that nothing was sent.
    {error:'unexpected_error',operation_started:false,requires_review:false},
    new Error('Lost HTTP response')]) {
    const f = fixture(result);
    await f.run('applyGroupMembers()');
    assert.equal(f.calls.length,1);
    assert.equal(f.run('groupOutcomes.get(300).blocked'),true);
    assert.equal(f.run('groupWritable(300)'),false);
    assert(f.run('groupResultHtml(300)').includes('Review Operations'));
    passed++;
  }
  const partial = fixture({physical_memberships_verified:false,message:'Stopped.',
    rows:[{target:40002,status:'needs_readback',transaction:'synthetic-receipt'}]},200);
  await partial.run('applyGroupMembers()');
  assert.equal(partial.calls.length,1);
  assert.equal(partial.run('groupOutcomes.get(300).blocked'),true);
  assert(partial.run('groupResultHtml(300)').includes('synthetic-receipt'));
  passed++;
  const removed = fixture({saved:true,revision:'updated'},200);
  removed.run('confirmRemoveGroup(300)');
  assert.equal(removed.calls.length,0,'confirmation must not send');
  assert.match(removed.modals[0],/No lights or controllers are reset/);
  assert.match(removed.modals[0],/group number is kept reserved/);
  await removed.run('removeEmptyGroup()');await removed.run('removeEmptyGroup()');
  assert.equal(removed.calls.length,1,'one removal submission');
  assert.deepStrictEqual(JSON.parse(removed.calls[0].options.body),{operation:'forget_empty_group',revision:'reviewed',target:300});
  assert(removed.notices.includes('drawer closed'));passed++;
  for (const scenario of ['nonempty','protected','all','stale','pending','disabled']) {
    const f=fixture({saved:true},200);
    if(scenario==='nonempty')f.group.members=[40002];
    if(scenario==='protected')f.group.protected=true;
    if(scenario==='all')f.group.group_id=24566;
    if(scenario==='stale')f.run('snapshotAt=-20000');
    if(scenario==='pending')f.state.status.pending_transactions=1;
    if(scenario==='disabled')f.run('feat=()=>false');
    f.run(`confirmRemoveGroup(${f.group.group_id})`);await f.run('removeEmptyGroup()');
    assert.equal(f.calls.length,0,scenario);assert.equal(f.modals.length,0,scenario);passed++;
  }
  const changed=fixture({saved:true},200);changed.run('confirmRemoveGroup(300)');changed.state.revision='different';
  await changed.run('removeEmptyGroup()');assert.equal(changed.calls.length,0);passed++;
  const uncertain=fixture(new Error('Lost response'));uncertain.run('confirmRemoveGroup(300)');
  await uncertain.run('removeEmptyGroup()');uncertain.run('confirmRemoveGroup(300)');await uncertain.run('removeEmptyGroup()');
  assert.equal(uncertain.calls.length,1);assert.equal(uncertain.run('groupRemovable(300)'),false);passed++;
  const refreshFailed=fixture({saved:true,revision:'updated'},200);refreshFailed.run("refreshSnapshot=async()=>{throw Error('Refresh lost')};confirmRemoveGroup(300)");
  await refreshFailed.run('removeEmptyGroup()');await refreshFailed.run('removeEmptyGroup()');
  assert.equal(refreshFailed.calls.length,1);assert.match(refreshFailed.run('groupOutcomes.get(300).message'),/Group removed, but the view could not refresh/);passed++;
  assert(source.includes('onclick=\"confirmRemoveGroup(${g.group_id})\"'),'group drawer exposes the reviewed action');
  console.log(`${passed} group admission and removal UI checks passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
