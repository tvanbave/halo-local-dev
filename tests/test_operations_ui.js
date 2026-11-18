// The shipped Operations view and device tools with synthetic fetch/DOM. No bridge is contacted.
const assert = require('assert'), fs = require('fs'), path = require('path'), vm = require('vm');
const web = name => fs.readFileSync(path.join(__dirname, '../avionmesh_pi/web', name), 'utf8');
const ui = web('ui.js'), ops = web('ops.js');
const api = ui.slice(ui.indexOf('const ERRORS ='), ui.indexOf('const getJson ='));
for (const name of ['ui.js', 'ops.js', 'ui.html', 'settings.js']) assert(!/classic/i.test(web(name)), name + ' must not mention the previous interface');

const TX = [
  {id:'t-edit', kind:'keypad_edit', target:40001, status:'uncertain', detail:{},
    recovery:{actions:['reconcile', 'resume_unattempted_insertion'], reason:'evidence_required; ambiguous writes are not retried'}},
  {id:'t-setup', kind:'keypad_setup', target:40001, status:'uncertain', detail:{}, recovery:{actions:['reconcile'], reason:'evidence_required; ambiguous writes are not retried'}},
  {id:'t-prep', kind:'group_membership', target:40002, status:'prepared', detail:{}, recovery:{actions:['cancel_unattempted'], reason:'no_dispatch_attempt_recorded'}},
  {id:'t-claim', kind:'claim', target:40005, status:'uncertain', detail:{}, recovery:{actions:['retire_pre_network_key_claim'], reason:'Stopped after the proposed address'}},
  {id:'t-wired', kind:'claim', target:40006, status:'uncertain', detail:{}, recovery:{actions:['reconcile_claim', 'verify_claim_owner_reset'], reason:'fresh_proof_required; no association retry'}},
  {id:'t-reset', kind:'accessory_setup', target:40007, status:'uncertain', detail:{}, recovery:{actions:['reconcile', 'observe_owner_reset'], reason:'evidence_required; ambiguous writes are not retried'}},
  {id:'t-dim', kind:'dimmer_edit', target:40007, status:'uncertain', detail:{}, recovery:{actions:['reconcile', 'accept_fresh_observation', 'future_step'], reason:'evidence_required; ambiguous writes are not retried'}},
  {id:'t-stuck', kind:'group_membership', target:40002, status:'uncertain', detail:{}, recovery:{actions:[], reason:'qualified_membership_readback_required'}},
  {id:'t-done', kind:'claim', target:40002, status:'verified', detail:{}, recovery:{actions:[], reason:'terminal_receipt_retained'}}];

function fixture({replies = {}, status = 200, confirmed = true, fresh = true, radio = null, caps = {}} = {}) {
  const calls = [];
  const state = {radio:{label:radio, queue:[]}, controllerViews:[], view:'operations',
    status:{mesh_ready:true, pending_transactions:8, jobs:{}, instance:'one', capabilities:caps}};
  const context = vm.createContext({S:state, Map, Set, Number, JSON, Date, Array, Object,
    esc:v => String(v ?? ''), I:{info:''}, confirm:() => confirmed, managementFresh:() => fresh,
    dev:id => id === 40001 ? {name:'Wall keypad', avion_id:id} : id === 40007 ? {name:'Bar dimmer', avion_id:id} : null,
    go:v => { state.view = v; }, renderSoon:() => {}, refreshSnapshot:async () => {}, sleep:async () => {},
    setTimeout:(fn) => { context.__later.push(fn); }, __later:[],
    fetch:async (url, options) => {
      const body = options && options.body ? JSON.parse(options.body) : undefined;
      calls.push({url, body});
      if (url === '/api/transactions') return {ok:true, status:200, json:async () => ({transactions:TX})};
      const reply = replies[url];
      if (reply instanceof Error) throw reply;
      const code = typeof status === 'object' ? (status[url] ?? 200) : status;
      return {ok:code < 400, status:code, json:async () => typeof reply === 'function' ? reply(state) : (reply ?? {})};
    }});
  vm.runInContext(api + ops, context);
  const run = code => vm.runInContext(code, context);
  const posts = () => calls.filter(c => c.url !== '/api/transactions');
  return {calls, posts, state, run, context};
}

(async () => {
  let passed = 0;
  const ok = name => { console.log('PASS ' + name); passed++; };

  let f = fixture();
  await f.run('loadOperations()');
  let html = f.run('viewOperations()');
  assert(html.includes('Needs review (8)') && html.includes('History (1)') && !html.includes('Finished and verified'));
  assert(html.includes('Keypad button change · Wall keypad (#40001)') && html.includes('Outcome not confirmed'));
  assert(html.includes('It can check without sending the change again'));
  assert(html.includes('so the light cannot have joined') && !html.includes('Stopped after the proposed address</p>'));
  assert(html.includes('Finish the button change') && html.includes('Close it (nothing was sent)'));
  assert(html.includes('The bridge offers no safe step'), 'an operation with no step is still shown');
  assert(html.includes('steps this page does not know: future_step'));
  f.run('Ops.showHistory = true');
  assert(f.run('viewOperations()').includes('Finished and verified'));
  ok('Lists unfinished operations in plain words, with history kept separate');

  const expected = [
    ['t-edit', 'reconcile', '/api/reconcile', {transaction:'t-edit'}],
    ['t-edit', 'resume_unattempted_insertion', '/api/keypad/resume', {transaction:'t-edit'}],
    ['t-prep', 'cancel_unattempted', '/api/cancel', {transaction:'t-prep'}],
    ['t-claim', 'retire_pre_network_key_claim', '/api/claim/pre-key-retire', {transaction:'t-claim'}],
    ['t-wired', 'reconcile_claim', '/api/reconcile', {transaction:'t-wired'}],
    ['t-wired', 'verify_claim_owner_reset', '/api/claim/reset-reconcile', {transaction:'t-wired', owner_reset:true}],
    ['t-reset', 'observe_owner_reset', '/api/reset-reconcile', {transaction:'t-reset', owner_reset:true}],
    ['t-dim', 'accept_fresh_observation', '/api/dimmer/accept', {transaction:'t-dim'}]];
  for (const [id, action, url, body] of expected) {
    f = fixture(); await f.run('loadOperations()');
    await f.run(`runOperation('${id}','${action}')`);
    assert.deepEqual(f.posts(), [{url, body}], action);
  }
  ok('Each offered step calls its own endpoint exactly once with the receipt id');

  f = fixture(); await f.run('loadOperations()');
  await f.run("runOperation('t-edit','cancel_unattempted')");      // not offered for this receipt
  await f.run("runOperation('t-dim','future_step')");              // unknown to this page
  await f.run("runOperation('missing','reconcile')");
  assert.equal(f.posts().length, 0);
  f = fixture({confirmed:false}); await f.run('loadOperations()');
  await f.run("runOperation('t-edit','reconcile')");
  assert.equal(f.posts().length, 0);
  for (const options of [{fresh:false}, {radio:'Pairing'}]) {
    f = fixture(options); await f.run('loadOperations()');
    assert.equal(f.run('opsAllowed()'), false);
    await f.run("runOperation('t-edit','reconcile')");
    assert.equal(f.posts().length, 0);
    assert(f.run('viewOperations()').includes(`onclick="runOperation('t-edit','reconcile')" disabled`));
  }
  ok('Steps the bridge did not offer, declined confirmations and busy or stale pages send nothing');

  const outcomes = [
    [{verified:true, write_retried:false}, 'Verified', false],
    [{verified:false, unchanged:true, requires_review:false}, 'Nothing was stored', false],
    [{verified:false, requires_review:true, resumable:true}, 'removal is confirmed', false],
    [{verified:false, requires_review:true}, 'Still not confirmed', true]];
  for (const [reply, text, review] of outcomes) {
    f = fixture({replies:{'/api/reconcile':reply}}); await f.run('loadOperations()');
    await f.run("runOperation('t-edit','reconcile')");
    assert(f.run("Ops.results.get('t-edit').text").includes(text), text);
    assert.equal(f.run("Ops.results.get('t-edit').review === true"), review);
    assert.equal(f.calls.filter(c => c.url === '/api/transactions').length, 2, 'list reloads after a step');
  }
  ok('Readback results are reported plainly and the list reloads');

  f = fixture({replies:{'/api/claim/pre-key-retire':{retired:true, pre_key_retired:true, address_retired:true, association_retried:false, automatic_reclaim:false}}});
  await f.run('loadOperations()'); await f.run("runOperation('t-claim','retire_pre_network_key_claim')");
  assert(f.run("Ops.results.get('t-claim').text").startsWith('Cleared.'));
  f = fixture({replies:{'/api/claim/pre-key-retire':{retired:true, pre_key_retired:true, address_retired:true, association_retried:true, automatic_reclaim:false}}});
  await f.run('loadOperations()'); await f.run("runOperation('t-claim','retire_pre_network_key_claim')");
  assert(f.run("Ops.results.get('t-claim').text").includes('Safe recovery is not confirmed'));
  ok('Pairing recovery is only reported clear when every safety field agrees');

  f = fixture({replies:{'/api/reconcile':{accepted:true, completed:false, job_id:'job-9', target:40001}}});
  await f.run('loadOperations()'); await f.run("runOperation('t-setup','reconcile')");
  assert.equal(f.run("Ops.results.get('t-setup').job"), 'job-9');
  f.state.status.jobs[40001] = {id:'other', state:'completed'}; f.run('settleOperationJobs()');
  assert.equal(f.run("Ops.results.get('t-setup').job"), 'job-9', 'a foreign job proves nothing');
  f.state.status.jobs[40001] = {id:'job-9', state:'needs_review'}; f.run('settleOperationJobs()');
  assert(f.run("Ops.results.get('t-setup').text").includes('could not confirm') && f.run("Ops.results.get('t-setup').review"));
  f = fixture({replies:{'/api/reconcile':{accepted:true, job_id:'job-9'}}});
  await f.run('loadOperations()'); await f.run("runOperation('t-setup','reconcile')");
  f.state.status.instance = 'two'; f.run('settleOperationJobs()');
  assert(f.run("Ops.results.get('t-setup').text").includes('bridge restarted'));
  f = fixture({replies:{'/api/reconcile':state => {
    state.status.instance = 'two';
    return {accepted:true, job_id:'old-process-job', target:40001};
  }}});
  await f.run('loadOperations()'); await f.run("runOperation('t-setup','reconcile')");
  assert.equal(f.run("Ops.results.get('t-setup').instance"), 'one', 'response retains its request-start process');
  assert.equal(f.run("Ops.results.get('t-setup').pending"), false);
  assert.equal(f.run("Ops.results.get('t-setup').job"), null);
  assert(f.run("Ops.results.get('t-setup').review"));
  assert(f.run("Ops.results.get('t-setup').text").includes('bridge restarted'));
  assert.equal(f.posts().length, 1, 'a restart never retries the request');
  ok('Background checks follow only their own job and survive a bridge restart honestly');

  for (const [reply, status, review, text] of [
    [{error:'mesh_busy', message:'Busy.', operation_started:false, requires_review:false, retry_allowed:false}, 409, false, 'Not started.'],
    [{error:'invalid_request'}, 400, true, 'not confirmed'],
    [{error:'operation_unavailable_or_uncertain'}, 409, true, 'not confirmed'],
    [new Error('Lost HTTP response'), 0, true, 'not confirmed']]) {
    f = fixture({replies:{'/api/reconcile':reply}, status}); await f.run('loadOperations()');
    await f.run("runOperation('t-edit','reconcile')");
    assert.equal(f.posts().length, 1, 'never an HTTP retry');
    assert(f.run("Ops.results.get('t-edit').text").includes(text), text);
    assert.equal(f.run("Ops.results.get('t-edit').review === true"), review);
  }
  f = fixture({replies:{'/api/keypad/resume':{error:'invalid_request'}}, status:400});
  await f.run('loadOperations()'); await f.run("runOperation('t-edit','resume_unattempted_insertion')");
  assert(f.run("Ops.results.get('t-edit').review"), 'HTTP400 can follow a transmitted insertion and failed readback');
  assert(f.run("Ops.results.get('t-edit').text").includes('not confirmed'));
  assert.equal(f.posts().length, 1);
  ok('Only explicit refusals count as not started; nothing is retried');

  f = fixture();
  f.state.controllerViews = [{device_id:40007, input_mode:'native_commands', input_status:'observing', actions:{local_input:{enabled:true}}}];
  html = f.run('accessoryInputHtml({avion_id:40007, product_type:91}, S.controllerViews[0])');
  assert(html.includes('native commands') && html.includes('setAccessoryInput(40007,true)') && !html.includes('disabled'));
  await f.run('setAccessoryInput(40007,true)');
  assert.deepEqual(f.posts(), [{url:'/api/controller/local-mode', body:{target:40007, enabled:true}}]);
  f.state.controllerViews[0].actions.local_input = {enabled:false, reason:'resolve_pending_operations'};
  html = f.run('accessoryInputHtml({avion_id:40007, product_type:91}, S.controllerViews[0])');
  assert(html.includes('disabled') && html.includes("go('operations')"));
  await f.run('setAccessoryInput(40007,false)');
  assert.equal(f.posts().length, 1);
  assert.equal(f.run('accessoryInputHtml({avion_id:40001, product_type:127}, S.controllerViews[0])'), '');
  ok('Accessory input mode changes only the interpretation, once, and only when allowed');

  f = fixture();
  html = f.run('deviceToolsHtml({avion_id:40007})');
  assert(html.includes('Inspect identity') && html.includes('Record 30 seconds') && !html.includes('removeDevice('));
  await f.run('removeDevice(40007)');
  assert.equal(f.posts().length, 0, 'removal needs the bridge capability');
  const removalReview = {target:40007, revision:'a'.repeat(64), eligible:true, enabled:true, affected_groups:[], native_dependencies:[]};
  f = fixture({caps:{unclaim:true}, replies:{'/api/unclaim/preview':removalReview, '/api/unclaim':{transaction:'x'}, '/api/examine':{model:91}}});
  assert(f.run('deviceToolsHtml({avion_id:40007})').includes('removeDevice(40007)'));
  await f.run('inspectDevice(40007)');
  assert(f.run('deviceToolsHtml({avion_id:40007})').includes('&quot;model&quot;: 91') || f.run('deviceToolsHtml({avion_id:40007})').includes('"model": 91'));
  await f.run('removeDevice(40007)');
  assert.deepEqual(f.posts(), [{url:'/api/examine', body:{target:40007}}, {url:'/api/unclaim/preview', body:{target:40007}}, {url:'/api/unclaim', body:{target:40007, revision:removalReview.revision}}]);
  assert(f.run('deviceToolsHtml({avion_id:40007})').includes('reviewOperation('));
  ok('Device tools: inspect any device; removal requires a reviewed revision and sends once');
  assert(f.run('deviceToolsHtml({avion_id:40007})').includes('automatic verification did not confirm'));
  f = fixture({caps:{unclaim:true}, replies:{'/api/unclaim/preview':removalReview,
    '/api/unclaim':async () => {
      assert(f.run('deviceToolsHtml({avion_id:40007})').includes('Sending removal and verifying'));
      await f.run('removeDevice(40007)');
      return {removed:true, verification:'verified', verification_attempts:2, publication_pending:false};
    }}});
  await f.run('removeDevice(40007)');
  assert.equal(f.posts().filter(c=>c.url==='/api/unclaim').length,1);
  assert(f.run('deviceToolsHtml({avion_id:40007})').includes('Removed and confirmed unpaired.'));
  assert.equal(f.run('opsTool(40007).review'),false);
  ok('Automatic removal verification shows progress, blocks repeats and reports success');


  for (const review of [
    {...removalReview, eligible:false, enabled:false, blockers:['active_relay_requires_reconnection']},
    {...removalReview, enabled:false}, {...removalReview, revision:'invalid'}, {...removalReview, target:40001}]) {
    f = fixture({caps:{unclaim:true}, replies:{'/api/unclaim/preview':review}});
    await f.run('removeDevice(40007)');
    assert.deepEqual(f.posts(), [{url:'/api/unclaim/preview', body:{target:40007}}]);
    assert.equal(f.run('opsTool(40007).busy'), false);
  }
  f = fixture({confirmed:false, caps:{unclaim:true}, replies:{'/api/unclaim/preview':removalReview}});
  await f.run('removeDevice(40007)');
  assert.deepEqual(f.posts(), [{url:'/api/unclaim/preview', body:{target:40007}}]);
  assert(f.run('opsTool(40007).message').includes('No removal request'));
  ok('Removal previews with blockers, unavailable capability, malformed identity or declined review never dispatch');

  f = fixture({caps:{unclaim:true}, replies:{'/api/unclaim/preview':removalReview, '/api/unclaim':new Error('Lost response')}});
  await f.run('removeDevice(40007)');
  assert.equal(f.posts().filter(c => c.url === '/api/unclaim').length, 1);
  assert(f.run('opsTool(40007).review') && f.run('opsTool(40007).message').includes('not confirmed'));
  f = fixture({caps:{unclaim:true}, replies:{'/api/unclaim/preview':removalReview, '/api/unclaim':{error:'invalid_request'}}, status:{'/api/unclaim':400}});
  await f.run('removeDevice(40007)');
  assert.equal(f.posts().filter(c => c.url === '/api/unclaim').length, 1);
  assert(f.run('opsTool(40007).review') && f.run('opsTool(40007).message').includes('not confirmed'));
  ok('Lost removal responses retain review without retry');
  for (const phase of ['preview', 'confirmation', 'response']) {
    f = fixture({caps:{unclaim:true}, replies:{
      '/api/unclaim/preview':state => {if(phase==='preview')state.status.instance='two';return removalReview;},
      '/api/unclaim':state => {if(phase==='response')state.status.instance='two';return {removed:true};}}});
    if(phase==='confirmation')f.context.confirm=()=>{f.state.status.instance='two';return true;};
    await f.run('removeDevice(40007)');
    assert.equal(f.posts().filter(c=>c.url==='/api/unclaim').length,phase==='response'?1:0);
    assert.equal(f.run('opsTool(40007).review'),phase==='response');
    assert(!f.run('opsTool(40007).message').includes('Removed and confirmed'));
  }
  ok('Removal pins its bridge instance across preview, confirmation and response');
  f = fixture({caps:{unclaim:true}, replies:{'/api/unclaim/preview':removalReview, '/api/unclaim':new Error('Lost response')}});
  await f.run('removeDevice(40007)');await f.run('removeDevice(40007)');
  assert.equal(f.posts().filter(c=>c.url==='/api/unclaim').length,1);
  assert(f.run('deviceToolsHtml({avion_id:40007})').includes('onclick="removeDevice(40007)" disabled'));
  ok('An uncertain removal cannot be submitted again from the device drawer');


  const importedCandidate = {uuid:'6655443322121400000a000000000001', product_id:134};
  const importedReceipt = {id:'imported-removal',kind:'remove',target:40007,status:'uncertain',
    detail:{identity_source:'cloud_import',product_id:134},
    recovery:{actions:['reconcile','confirm_imported_removal'],reason:'imported_removal_owner_confirmation'}};
  const ownerNeeded = {removed:false,owner_confirmation_required:true,requires_review:true,
    verification:'owner_confirmation_required',transaction:importedReceipt.id,removal_candidates:[importedCandidate]};
  let removalConfirmation='';
  f=fixture({caps:{unclaim:true}, replies:{'/api/unclaim/preview':{...removalReview,
    identity_source:'cloud_import',requires_owner_confirmation:true},'/api/unclaim':ownerNeeded}});
  f.context.confirm=text=>{removalConfirmation=text;return true;};
  await f.run('removeDevice(40007)');
  assert(removalConfirmation.includes('cloud-imported')&&removalConfirmation.includes('identify and confirm'));
  assert(f.run('opsTool(40007).message').includes('cannot automatically match'));
  assert(!f.run('opsTool(40007).message').includes('Removed and confirmed'));
  f.run(`Ops.list=${JSON.stringify([importedReceipt])}`);
  html=f.run('opsCard(Ops.list[0])');
  assert(html.includes('This is the light I saw reset')&&html.includes('12:22:33:44:55:66'));
  assert(!html.includes('steps this page does not know'));
  assert(html.includes('does not prove which light was reset'));
  assert.equal(f.posts().filter(c=>c.url==='/api/unclaim').length,1);
  ok('Imported removal discloses manual identity confirmation and stays pending after new unpaired evidence');

  function readyImported(options={}) {
    const fixtureResult=fixture(options);
    fixtureResult.run(`Ops.list=${JSON.stringify([importedReceipt])};Ops.results.set('imported-removal', {...opsOutcome(Ops.list[0],{},${JSON.stringify(ownerNeeded)}),instance:'one'})`);
    return fixtureResult;
  }
  for(const change of ['foreign-candidate','stale-instance','not-offered','declined','busy','ambiguous']) {
    f=readyImported();let candidate=importedCandidate.uuid;
    if(change==='foreign-candidate')candidate='ff'.repeat(16);
    if(change==='stale-instance')f.state.status.instance='two';
    if(change==='not-offered')f.run("Ops.list[0].recovery.actions=['reconcile']");
    if(change==='declined')f.context.confirm=()=>false;
    if(change==='busy')f.state.radio.label='Pairing';
    if(change==='ambiguous') {
      f.run(`Ops.results.get('imported-removal').removal_candidates.push({uuid:'ff'.repeat(16),product_id:134})`);
      const ambiguous=f.run('opsCard(Ops.list[0])');
      assert(ambiguous.includes('More than one newly unpaired light'));
      assert(ambiguous.includes(`onclick="confirmImportedRemoval('imported-removal','${importedCandidate.uuid}')" disabled`));
    }
    await f.run(`confirmImportedRemoval('imported-removal','${candidate}')`);
    assert.equal(f.posts().length,0,change);
  }
  f=readyImported();f.run("Ops.results.clear()");
  assert(!f.run('opsCard(Ops.list[0])').includes('onclick="confirmImportedRemoval'));
  assert(f.run('opsCard(Ops.list[0])').includes('Check what happened'));
  ok('Imported confirmation requires offered fresh candidates, current bridge, idle UI and explicit owner agreement');

  f=readyImported({replies:{'/api/unclaim/confirm':{removed:true,verification_mode:'owner_confirmed_unpaired'}}});
  await f.run(`confirmImportedRemoval('imported-removal','${importedCandidate.uuid}')`);
  assert.deepEqual(f.posts(),[{url:'/api/unclaim/confirm',body:{transaction:'imported-removal',uuid:importedCandidate.uuid,owner_confirmed:true}}]);
  assert(f.run("Ops.results.get('imported-removal').text").includes('using your confirmation'));
  assert(!f.run("Ops.results.get('imported-removal').text").includes('hardware identity verified'));
  f=readyImported({replies:{'/api/unclaim/confirm':new Error('Connection lost')}});
  await f.run(`confirmImportedRemoval('imported-removal','${importedCandidate.uuid}')`);
  assert(f.run("Ops.results.get('imported-removal').review"));
  assert(f.run("Ops.results.get('imported-removal').text").includes('Nothing was reset again'));
  await f.run(`confirmImportedRemoval('imported-removal','${importedCandidate.uuid}')`);
  assert.equal(f.posts().length,1,'uncertain confirmation requires a new read rather than repeated submission');
  ok('Imported confirmation submits one read-only request and reports owner-assisted evidence truthfully');

  for(const phase of ['preview','submit']) {
    const error={error:'removal_identity_unavailable',operation_started:false,requires_review:false,
      message:'The bridge has no complete pairing identity or supported cloud-import record for this device. No removal was sent.'};
    f=fixture({caps:{unclaim:true},replies:{'/api/unclaim/preview':phase==='preview'?error:removalReview,
      '/api/unclaim':error},status:{[phase==='preview'?'/api/unclaim/preview':'/api/unclaim']:409}});
    await f.run('removeDevice(40007)');
    assert(f.run('opsTool(40007).message').includes('cloud-import record'));
    assert(!f.run('opsTool(40007).removalUncertain'));
    assert.equal(f.posts().filter(c=>c.url==='/api/unclaim').length,phase==='preview'?0:1);
  }
  f=fixture({caps:{unclaim:true},replies:{'/api/unclaim/preview':removalReview,
    '/api/unclaim':{error:'pending_operation',operation_started:false,requires_review:true,message:'Review Operations.'}},status:{'/api/unclaim':409}});
  await f.run('removeDevice(40007)');
  assert(f.run('opsTool(40007).review'));
  assert(!f.run('opsTool(40007).removalUncertain'));
  ok('Specific pre-dispatch errors explain the refusal and preserve required Operations review');

  f=fixture();
  f.run(`Ops.list=[{id:'removed',kind:'remove',target:40007,status:'verified',detail:{publication_pending:true},recovery:{actions:['refresh_removed_publication'],reason:'removal_publication_pending'}}]`);
  assert(f.run('viewOperations()').includes('Needs review (1)'));
  assert(f.run('viewOperations()').includes('History (0)'));
  assert(f.run('viewOperations()').includes('Refresh publication cleanup'));
  await f.run("runOperation('removed','refresh_removed_publication')");
  assert.deepEqual(f.posts(),[{url:'/api/reconcile',body:{transaction:'removed'}}]);
  const outcome=f.run("opsOutcome({}, {}, {removed:true,publication_pending:true})");
  assert(outcome.review&&outcome.text.includes('Removal is confirmed'));
  ok('Confirmed removal with pending publication stays actionable in Needs review');



  f = fixture();
  html = f.run('opsSettingsCards() + opsInventoryCard()');
  assert(html.includes('8 unfinished operations need review') && html.includes("go('operations')") && html.includes('Export list') && html.includes('Import list'));
  f.state.status.pending_transactions = 0;
  assert(f.run('opsSettingsCards()').includes('No unfinished device changes'));
  f.state.status.publication_pending = 1;
  assert(f.run('opsSettingsCards()').includes('1 confirmed removal needs publication cleanup'));
  assert(f.run('opsSettingsCards()').includes('Review operations'));
  await f.run('loadOperations()');
  const scheduled=f.context.__later.length;
  f.state.status.publication_pending=2;
  f.run('viewOperations()');
  assert.equal(f.context.__later.length,scheduled+1,'cleanup count changes refresh the operation list');
  assert.equal(f.state.status.pending_transactions,0,'cleanup is not an uncertain mesh operation');
  ok('Settings shows the operation count and the device list backup');

  for (const product of [126,81]) {
    let complete, confirmation='';
    f=fixture({replies:{'/api/reconcile':()=>new Promise(resolve=>{complete=resolve;})}});
    const claim={id:'sleeping-claim',kind:'claim',target:40126,status:'uncertain',
      detail:{product_id:product,uuid_hash:123456,association_complete:true},recovery:{actions:['reconcile_claim']}};
    f.context.claim=claim;f.run('Ops.list=[claim]');
    f.context.confirm=text=>{confirmation=text;return true;};
    const checking=f.run("runOperation('sleeping-claim','reconcile_claim')");
    for(let i=0;i<10&&!complete;i++)await Promise.resolve();
    assert(complete,'verification request must remain pending');
    assert(confirmation.includes('Wait for the wake prompt'));
    assert(confirmation.includes(product===126?'Power/Off':'small Wake-Up'));
    assert(!f.run('opsCard(claim)').includes('once now'));
    f.state.status.claim_progress={40126:{phase:'waiting_for_wake',uuid_hash:123456}};
    assert(f.run('opsCard(claim)').includes(product===126?'Power/Off button once now':'small Wake-Up button on the bottom once now'));
    for(const [phase,text] of [['settling','Let the controller settle'],['verifying','Checking the encrypted pairing reply'],
      ['verified','Finishing the device list update'],['needs_review','Pairing verification is not confirmed']]){
      f.state.status.claim_progress[40126].phase=phase;
      assert(f.run('opsCard(claim)').includes(text),phase);
    }
    f.state.status.claim_progress[40126]={phase:'waiting_for_wake',uuid_hash:999};
    assert(!f.run('opsCard(claim)').includes('once now'),'wrong hash must not prompt');
    f.state.status.claim_progress={40127:{phase:'waiting_for_wake',uuid_hash:123456}};
    assert(!f.run('opsCard(claim)').includes('once now'),'wrong target must not prompt');
    f.state.status.claim_progress={40126:{phase:'waiting_for_wake',uuid_hash:123456}};
    f.run("Ops.busy='other-claim'");assert(!f.run('opsCard(claim)').includes('once now'));
    f.run("Ops.busy='sleeping-claim';Ops.results.get('sleeping-claim').target=40127");
    assert(!f.run('opsCard(claim)').includes('once now'));
    f.run("Ops.results.get('sleeping-claim').target=40126;Ops.results.get('sleeping-claim').pending=false");
    assert(!f.run('opsCard(claim)').includes('once now'));
    f.run("Ops.results.get('sleeping-claim').pending=true");
    f.state.status.instance='two';assert(!f.run('opsCard(claim)').includes('once now'));
    assert(f.run('opsCard(claim)').includes('bridge restarted during verification'));
    f.state.status.instance='one';
    await f.run("runOperation('sleeping-claim','reconcile_claim')");
    assert.equal(f.posts().length,1,'pending verification never submits again');
    complete({verified:true,associated:true,device_id:40126,association_retried:false});await checking;
    assert(f.run("Ops.results.get('sleeping-claim').text").includes('Pairing verified. The device was added'));
    assert(!f.run('opsCard(claim)').includes('once now'),'finished operation must not retain a wake prompt');
  }
  ok('Pending sleeping-controller checks show only matching receipt, target, hash and instance progress');
  f=fixture();
  f.context.claim={kind:'claim',target:40126,detail:{product_id:126}};
  const failed=f.run('opsOutcome(claim,{}, {verified:false,requires_review:true,association_retried:false})');
  assert(failed.review&&failed.text.includes('Pairing is still not confirmed'));
  assert(!failed.text.includes('reset the device'));
  const pendingPublication=f.run('opsOutcome(claim,{}, {verified:true,associated:true,device_id:40126,publication_pending:true})');
  assert(pendingPublication.review&&pendingPublication.text.includes('Publication still needs review'));
  const unknownEntry=f.run('opsOutcome(claim,{}, {verified:true})');
  assert(unknownEntry.text.includes('Pairing verified')&&!unknownEntry.text.includes('was added'));
  ok('Claim outcomes distinguish verified pairing, publication review and unconfirmed checks without recommending reset');

  f = fixture();
  assert.equal(f.run('OPS_KINDS.claim_pre_key'), 'Unpaired check after a stopped pairing');
  assert.equal(f.run('OPS_STATUS.retired'), 'Retired; record kept');
  html = f.run("opsCard({id:'p1', kind:'claim_pre_key', target:40009, status:'retired', detail:{}, recovery:{actions:[], reason:'terminal_receipt_retained'}})");
  assert(!/claim_pre_key ·|>retired</.test(html) && html.includes('Retired; record kept'));
  ok('History uses plain labels for stopped-pairing checks and retired records');

  // Scene receipts: plain labels and the same explicit steps as the Scenes page.
  f = fixture();
  const sceneCalls = [];
  Object.assign(f.context, {sceneRunning:() => false, sceneLightName:id => 'Light ' + id,
    sceneOp:id => id === 's-live' ? {progress:{action:'create', desired:{name:'Evening'}, recovery_ready:true, unreachable:40002, steps:[]}} : null,
    sceneOpDetail:op => op.progress, recoverScene:id => sceneCalls.push(['recover', id]),
    resumeScene:id => sceneCalls.push(['resume', id]), confirmSetAsideScene:id => sceneCalls.push(['aside', id])});
  html = f.run("opsCard({id:'s-live', kind:'scene', target:40001, status:'uncertain', detail:{action:'create', desired:{name:'Evening'}, recovery_ready:false, steps:[]}, recovery:{actions:['reconcile'], reason:'read_only_scene_property_verification'}})");
  assert(html.includes('Scene save · Evening') && html.includes('Needs review'));
  assert(!/read_only_scene_property_verification<\/p>|>scene ·|Outcome not confirmed|runOperation/.test(html), 'no raw labels and no synchronous check');
  assert(html.includes("onclick=\"resumeScene('s-live')\"") && html.includes('>Continue save<'), 'uses the live record, which is ready to continue');
  assert(html.includes("onclick=\"recoverScene('s-live')\"") && html.includes("onclick=\"confirmSetAsideScene('s-live')\""));
  assert(html.includes('Light 40002 did not answer the last check.'));
  f.state.status.mesh_ready = false;
  html = f.run("opsCard({id:'s-live', kind:'scene', target:40001, status:'uncertain', detail:{}, recovery:{actions:['reconcile']}})");
  assert(/recoverScene\('s-live'\)" disabled/.test(html) && /confirmSetAsideScene\('s-live'\)" >/.test(html), 'setting aside needs no mesh; reading does');
  html = f.run("opsCard({id:'s-old', kind:'scene', target:40001, status:'retired', detail:{action:'edit', prior:{name:'Movie'}, desired:{name:'Movie'}, outcome:'set_aside', set_aside:{reference_reserved:true}}, recovery:{actions:[]}})");
  assert(html.includes('Scene change · Movie') && html.includes('Set aside; record kept') && html.includes('slot stays reserved'));
  assert(!html.includes('<button class="btn'), 'a closed scene record offers no steps');
  html = f.run("opsCard({id:'s-ok', kind:'scene', target:40001, status:'verified', detail:{action:'remove', prior:{name:'Movie'}, desired:null, completed:true}, recovery:{actions:[]}})");
  assert(html.includes('Scene removal · Movie') && html.includes('Finished and verified'));
  ok('Scene operations use plain labels and offer read, continue and set aside');

  console.log(JSON.stringify({passed:true, checks:passed}));
})().catch(e => { console.error(e); process.exit(1); });
