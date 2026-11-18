// The shipped keypad button editor with synthetic fetch/DOM. No bridge is contacted.
const assert = require('assert'), fs = require('fs'), path = require('path'), vm = require('vm');
const source = fs.readFileSync(path.join(__dirname, '../avionmesh_pi/web/ui.js'), 'utf8');
const api = source.slice(source.indexOf('const ERRORS ='), source.indexOf('const getJson ='));
const editor = source.slice(source.indexOf('/* ---------- keypad button editor'), source.indexOf('/* ---------- controller details'));
const details = source.slice(source.indexOf('/* ---------- controller details'), source.indexOf('function ctrlDrawer('));
const attention = source.slice(source.indexOf('/* ---------- attention'), source.indexOf('/* ---------- views'));
const lightingGroups = source.match(/^const lightingGroups = .*;$/m)[0];
assert(api.length > 100 && editor.length > 1000, 'editor section markers must exist');

const GROUPS = [{group_id:291, name:'Theatre', members:[1], protected:false},
  {group_id:293, name:'Bar', members:[2], protected:false},
  {group_id:24575, name:'HA keypad target', members:[], protected:true}];
function view(overrides = {}) {
  return {device_id:40001, product_id:127, assignment_read_back:true, revision:'rev-1',
    actions:{edit_assignment:{enabled:true, reason:'available'}},
    slots:[{slot:1, target:291, preset:200, status:'verified_mapping', editable:true},
      {slot:2, target:65535, preset:0, status:'unused', editable:true},
      {slot:3, target:24575, preset:255, status:'verified_mapping', editable:false},
      {slot:4, target:65535, preset:0, status:'unused', editable:true}], ...overrides};
}
function fixture({result = {job_id:'job-1'}, status = 202, confirmed = true, features = {keypad_edit:true}, v = view()} = {}) {
  const calls = [];
  const state = {synced:true, radio:{label:null, queue:[]}, groups:GROUPS, controllerViews:[v],
    caps:{features}, status:{mesh_ready:true, mqtt_enabled:true, mqtt_ready:true, jobs:{}, keypad_saves:{}, instance:'one'}};
  const context = vm.createContext({S:state, Map, Set, Number, JSON,
    feat:k => state.caps.features[k], pct:b => Math.round(b / 255 * 100),
    gName:id => (GROUPS.find(g => g.group_id === id) || {name:'Group ' + id}).name,
    dev:() => ({name:'Synthetic keypad'}), grp:id => GROUPS.find(g => g.group_id === id), esc:String, confirm:() => confirmed, accessoryInputHtml:()=>'', opsReviewButton:(label='Review Operations')=>`<button class="btn" onclick="go('operations')">${label}</button>`,
    managementFresh:() => state.synced, groupBusy:false, sleep:async () => {},
    lights:() => [], controllers:() => [], dupNames:() => [],
    refreshSnapshot:async () => {}, renderSoon:() => {},
    fetch:async (url, options) => {
      calls.push({url, body:options && JSON.parse(options.body)});
      if (result instanceof Error) throw result;
      return {ok:status < 400, status, json:async () => result};
    }});
  vm.runInContext(lightingGroups + '\n' + api + editor + details + attention, context);
  return {calls, state, run:code => vm.runInContext(code, context)};
}
const D = '({avion_id:40001})';

(async () => {
  let passed = 0;
  const ok = name => { console.log('PASS ' + name); passed++; };

  for (const f of [fixture({features:{keypad_edit:false}}), fixture({v:view({assignment_read_back:false})}),
    fixture({v:view({actions:{edit_assignment:{enabled:false, reason:'model_editor_disabled'}}})})]) {
    assert.equal(f.run('keypadEditable(40001)'), false);
    assert.equal(f.run('keypadSaveAllowed(40001)'), false);
  }
  ok('Editor stays hidden without the capability, a verified read or an enabled action');

  let f = fixture();
  assert.deepEqual(f.run('keypadChanges(40001)'), []);
  assert.equal(f.run('keypadSaveAllowed(40001)'), false);
  let html = f.run(`keypadEditorHtml(${D}, keypadView(40001))`);
  assert(html.includes('78% (saved)'), 'a saved raw level is offered unchanged');
  assert(html.includes('Protected or unknown assignment · HA keypad target (cannot be changed here)') && !html.includes('value="24575"'), 'unowned protected targets are not presented as editable HA buttons');
  assert(html.includes('Save changes') && html.includes('disabled'));
  ok('Unchanged table offers no save and never lists protected targets');

  f.run("setKeypadChoice(40001,2,'group','293')");
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('keypadChanges(40001)'))), [{slot:2, mode:'native', group:293}]);
  f.run("setKeypadChoice(40001,2,'preset','128')");
  f.run("setKeypadChoice(40001,1,'group','0')");
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('keypadChanges(40001)'))),
    [{slot:1, mode:'unassigned'}, {slot:2, mode:'native', group:293, brightness:128}]);
  f.run("setKeypadChoice(40001,3,'group','293')");
  assert.equal(f.run('keypadChanges(40001)').length, 2, 'read-only button cannot be drafted');
  ok('Drafts become exact typed changes; an untouched level is not sent');

  f = fixture();
  f.run("setKeypadChoice(40001,1,'preset','255')");
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('keypadChanges(40001)'))), [{slot:1, mode:'native', group:291, brightness:255}]);
  f.run("setKeypadChoice(40001,1,'preset','200')");
  assert.deepEqual(f.run('keypadChanges(40001)'), []);
  ok('Level-only edit is explicit and returning to the saved level is a no-op');

  f = fixture();
  f.run("setKeypadChoice(40001,2,'group','291')");
  assert.equal(f.run('keypadDuplicate(40001)'), 291);
  assert.equal(f.run('keypadSaveAllowed(40001)'), false);
  await f.run('saveKeypad(40001)');
  assert.equal(f.calls.length, 0);
  ok('One group on two buttons blocks the save before any request');

  f = fixture({confirmed:false});
  f.run("setKeypadChoice(40001,2,'group','293')");
  await f.run('saveKeypad(40001)');
  assert.equal(f.calls.length, 0);
  ok('Declined confirmation sends nothing');

  f = fixture();
  f.run("setKeypadChoice(40001,2,'group','293')");
  assert(f.run("keypadChangeText(40001, keypadChanges(40001)[0])").includes('Button 2: Unused → Bar at 100%'));
  await f.run('saveKeypad(40001)');
  assert.deepEqual(f.calls, [{url:'/api/keypad/save', body:{target:40001, revision:'rev-1', changes:[{slot:2, mode:'native', group:293}]}}]);
  assert.equal(f.run('keypadSaves.get(40001).busy'), true);
  assert.equal(f.run('keypadSaveAllowed(40001)'), false, 'no second save while one runs');
  await f.run('saveKeypad(40001)');
  assert.equal(f.calls.length, 1);
  f.state.status.jobs[40001] = {id:'job-1', state:'running'};
  f.state.status.keypad_saves[40001] = {state:'running', buttons:[{slot:2, state:'writing'}]};
  html = f.run('keypadSaveStatus(40001)');
  assert(html.includes('Button 2: Writing and checking'));
  f.state.status.jobs[40001] = {id:'job-1', state:'completed', result:{changed:true, verified:true}};
  f.state.status.keypad_saves[40001] = {state:'verified', buttons:[{slot:2, state:'verified'}]};
  html = f.run('keypadSaveStatus(40001)');
  assert(html.includes('saved and verified') && html.includes('Button 2: Saved and verified') && !html.includes("go('operations')"));
  assert.equal(f.run('keypadDrafts.has(40001)'), false);
  ok('One request carries the reviewed revision; progress and completion come from the bridge');

  f = fixture();
  f.run("setKeypadChoice(40001,2,'group','293')");
  const busySlots = view().slots.map(row => ({...row, editable:false, ha_available:false,
    draft_editable:row.editable, ha_draft_available:row.editable}));
  f.state.controllerViews[0] = view({editor_available:true,slots:busySlots,
    actions:{edit_assignment:{enabled:false,reason:'operation_in_progress'}}});
  assert.equal(f.run('keypadEditorVisible(40001)'),true);
  assert.equal(f.run('keypadSaveAllowed(40001)'),false);
  html = f.run('controllerDetails({avion_id:40001, product_type:127})');
  assert(html.includes('id="kpM1"') && html.includes('id="kpM2"') && !html.includes('id="kpM3"'));
  assert(html.includes('Your selections are kept'));
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('keypadChanges(40001)'))),[{slot:2,mode:'native',group:293}]);
  await f.run('saveKeypad(40001)');assert.equal(f.calls.length,0);
  f.run("setKeypadChoice(40001,2,'mode','ha')");
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('keypadChanges(40001)'))),[{slot:2,mode:'ha'}]);
  f.state.controllerViews[0] = view({editor_available:false,slots:busySlots.map(row=>({...row,draft_editable:false,ha_draft_available:false})),
    actions:{edit_assignment:{enabled:false,reason:'resolve_pending_operations'}}});
  html=f.run('controllerDetails({avion_id:40001, product_type:127})');
  assert(!html.includes('class="kp-edit"') && html.includes('Review unfinished operations'));
  await f.run('saveKeypad(40001)');assert.equal(f.calls.length,0);
  ok('Busy preserves drafts and all eligible rows without weakening save or pending-operation admission');

  f = fixture();
  f.run("setKeypadChoice(40001,2,'group','293')");
  await f.run('saveKeypad(40001)');
  f.state.status.jobs[40001] = {id:'job-1', state:'needs_review'};
  f.state.status.keypad_saves[40001] = {state:'needs_review', buttons:[{slot:2, state:'needs_review'}]};
  html = f.run('keypadSaveStatus(40001)');
  assert(html.includes('could not be confirmed') && html.includes("go('operations')") && html.includes('Button 2: Needs review'));
  assert.equal(f.calls.length, 1);
  ok('Unconfirmed button points to review and is never resent');

  for (const invalidated of [true, false]) {
    f = fixture();
    f.run("setKeypadChoice(40001,2,'group','293')");
    await f.run('saveKeypad(40001)');
    f.state.controllerViews[0] = view({assignment_read_back:!invalidated,
      revision:invalidated ? null : 'rev-1', slots:invalidated ? [] : view().slots.map(row => ({...row, editable:false})),
      actions:{edit_assignment:{enabled:false, reason:invalidated ? 'operation_in_progress' : 'resolve_pending_operations'}}});
    f.state.status.jobs[40001] = {id:'job-1', state:'running'};
    f.state.status.keypad_saves[40001] = {state:'running', buttons:[{slot:2, state:'writing'}]};
    html = f.run('controllerDetails({avion_id:40001, product_type:127})');
    assert(html.includes('Save accepted') && html.includes('Button 2: Writing and checking'));
    assert(!html.includes('class="kp-edit"'), 'writing controls stay hidden when edit admission is unavailable');
    assert.equal((html.match(/role="status"/g) || []).length, 1, 'save status is rendered once');
    f.state.status.jobs[40001] = {id:'job-1', state:'needs_review'};
    f.state.status.keypad_saves[40001] = {state:'needs_review', buttons:[{slot:2, state:'needs_review'}]};
    html = f.run('controllerDetails({avion_id:40001, product_type:127})');
    assert(html.includes('could not be confirmed') && html.includes('Review Operations'));
    assert(html.includes('Button 2: Needs review'));
    assert.equal(f.run('keypadSaves.get(40001).busy'), false);
    assert.equal(f.calls.length, 1);
  }
  ok('Drawer keeps save progress and recovery visible without an editable binding');

  f = fixture();
  f.run("setKeypadChoice(40001,2,'group','293')");
  await f.run('saveKeypad(40001)');
  f.state.controllerViews[0] = view({assignment_read_back:false, revision:null, slots:[],
    actions:{edit_assignment:{enabled:false, reason:'read_assignments_first'}}});
  f.state.status.instance = 'two';
  html = f.run('controllerDetails({avion_id:40001, product_type:127})');
  assert(html.includes('bridge restarted') && html.includes('Review Operations'));
  assert.equal(f.calls.length, 1);
  ok('Drawer reports interrupted save after restart without a verified binding');

  f = fixture();
  f.run("setKeypadChoice(40001,2,'group','293')");
  await f.run('saveKeypad(40001)');
  f.state.status.jobs[40001] = {id:'other-job', state:'completed'};
  assert(f.run('keypadSaveStatus(40001)').includes('Save accepted'), 'a foreign job proves nothing');
  f.state.status.instance = 'two';
  html = f.run('keypadSaveStatus(40001)');
  assert(html.includes('bridge restarted') && html.includes("go('operations')"));
  ok('Foreign job is ignored and a bridge restart asks for a fresh read');

  for (const [result, status, review, text] of [
    [{error:'mesh_busy', message:'Busy.', operation_started:false, requires_review:false, retry_allowed:false}, 409, false, 'Save not started'],
    [{error:'pending_operation', message:'Pending.', operation_started:false, requires_review:true, retry_allowed:false}, 409, true, 'Save not started'],
    [{error:'invalid_request'}, 400, false, 'Read the buttons again'],
    [{error:'keypad_editor_disabled'}, 501, false, 'Save not started'],
    [{error:'operation_unavailable_or_uncertain'}, 409, true, 'outcome not confirmed'],
    [new Error('Lost HTTP response'), 0, true, 'outcome not confirmed']]) {
    f = fixture({result, status});
    f.run("setKeypadChoice(40001,2,'group','293')");
    await f.run('saveKeypad(40001)');
    assert.equal(f.calls.length, 1, 'never an HTTP retry');
    assert.equal(f.run('keypadSaves.get(40001).busy'), false);
    assert.equal(f.run('keypadSaves.get(40001).review === true'), review, JSON.stringify(result));
    assert(f.run('keypadSaves.get(40001).message').includes(text), text);
  }
  ok('Only explicit refusals are reported as not started; anything else needs review');

  f = fixture();
  f.run("setKeypadChoice(40001,2,'group','293')");
  f.state.controllerViews[0].revision = 'rev-2';
  assert.deepEqual(f.run('keypadChanges(40001)'), []);
  ok('A new table revision discards the old draft');

  f = fixture();
  f.state.controllerViews[0].slots[0].ha_available = true;
  f.run("setKeypadChoice(40001,1,'mode','ha')");
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('keypadChanges(40001)'))), [{slot:1,mode:'ha'}]);
  assert(f.run('keypadChangeText(40001, keypadChanges(40001)[0])').includes('Theatre at 78% → Home Assistant (automation required)'));
  html = f.run(`keypadEditorHtml(${D},keypadView(40001))`);
  assert(!html.includes('id="kpG1"') && html.includes('No direct light control'));
  await f.run('saveKeypad(40001)');
  assert.deepEqual(f.calls[0].body.changes,[{slot:1,mode:'ha'}]);
  assert.equal(f.state.controllerViews[0].slots[2].target,24575);
  ok('Native to HA sends only the selected slot and removes native target and brightness controls');

  const owned = view();
  Object.assign(owned.slots[0],{target:24574,preset:255,assignment_mode:'ha',ha_owned:true,ha_available:true,ha_ready:true});
  f = fixture({v:owned});
  assert.equal(f.run('keypadChoice(40001,keypadView(40001).slots[0]).mode'),'ha');
  assert.deepEqual(f.run('keypadChanges(40001)'),[]);
  assert(f.run('keypadAssignmentText(keypadView(40001).slots[0])') === 'Home Assistant');
  f.run("setKeypadChoice(40001,1,'mode','native')");
  assert.equal(f.run('keypadSaveAllowed(40001)'),false,'a direct destination must be selected');
  f.run("setKeypadChoice(40001,1,'group','293')");
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('keypadChanges(40001)'))),[{slot:1,mode:'native',group:293}]);
  assert(f.run('keypadChangeText(40001,keypadChanges(40001)[0])').includes('Home Assistant → Bar at 100%'));
  f.run("setKeypadChoice(40001,1,'mode','unassigned')");
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('keypadChanges(40001)'))),[{slot:1,mode:'unassigned'}]);
  ok('Owned HA mode persists and can be reviewed back to a direct group or unused');

  f = fixture();
  f.run("setKeypadChoice(40001,1,'mode','ha')");
  assert.deepEqual(f.run('keypadChanges(40001)'),[],'HA needs backend availability');
  f.state.controllerViews[0].slots[0].ha_available=true;
  f.run("setKeypadChoice(40001,1,'mode','ha')");
  f.state.controllerViews[0].slots[0].ha_available=false;
  assert.equal(f.run('keypadSaveAllowed(40001)'),false,'availability can change while a draft is open');
  ok('Unavailable or newly unavailable HA provisioning never submits a save');

  f=fixture();
  Object.assign(f.state.controllerViews[0].slots[2],{assignment_mode:'ha',ha_owned:false,editable:true,ha_available:true});
  f.run("setKeypadChoice(40001,3,'mode','unassigned')");
  assert.deepEqual(f.run('keypadChanges(40001)'),[]);
  assert(!f.run(`keypadEditorHtml(${D},keypadView(40001))`).includes('id="kpM3"'));
  f.state.controllerViews[0].slots[0].ha_available=true;
  f.run("setKeypadChoice(40001,1,'mode','ha')");
  f.state.synced=false;
  await f.run('saveKeypad(40001)');
  assert.equal(f.calls.length,0);
  ok('HA ownership must be explicit and a stale snapshot cannot submit a draft');

  f=fixture();
  f.state.groups=[...GROUPS,{group_id:295,name:'Empty ordinary group',members:[],protected:false}];
  assert.deepEqual(Array.from(f.run('attentionItems().map(item=>item.t)')),['"Empty ordinary group" has no lights']);
  ok('Protected empty HA targets never prompt users to add lights or delete them');

  for (const [status,phrase] of [['disabled','Set it up in Settings'],['disconnected','until it reconnects'],
    ['not_exposed','Choose Add to Home Assistant below'],['publishing_enabled','actual Home Assistant receipt is not verified here']]) {
    f=fixture({v:view({ha_status:status})});
    f.state.controllerViews[0].slots[0].ha_available=true;
    f.run("setKeypadChoice(40001,1,'mode','ha')");
    assert.equal(f.run('keypadSaveAllowed(40001)'),true,'MQTT and sharing do not gate provisioning');
    assert(f.run('controllerDetails({avion_id:40001,product_type:127})').includes(phrase));
  }
  ok('Readiness distinguishes MQTT setup, disconnected, unshared and event publication');

  f=fixture({result:new Error('Lost HTTP response')});
  f.state.controllerViews[0].slots[0].ha_available=true;
  f.run("setKeypadChoice(40001,1,'mode','ha')");
  await f.run('saveKeypad(40001)');
  f.run("setKeypadChoice(40001,1,'mode','native')");
  await f.run('saveKeypad(40001)');
  assert.equal(f.calls.length,1);
  assert.equal(f.run('keypadSaves.get(40001).review'),true);
  assert.equal(f.run('keypadSaveAllowed(40001)'),false);
  f.run("controllerReads.set(40001,{job:'read-2',instance:'one'})");
  f.state.status.jobs[40001]={id:'read-2',state:'completed'};
  f.run('controllerDetails({avion_id:40001,product_type:127})');
  assert.equal(f.run('keypadSaves.has(40001)'),false);
  assert.deepEqual(f.run('keypadChanges(40001)'),[]);
  ok('Uncertain HA save stays locked until an explicit successful fresh read');

  console.log(JSON.stringify({passed:true, checks:passed}));
})().catch(e => { console.error(e); process.exit(1); });
