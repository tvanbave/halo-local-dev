// Controller mode reviews with synthetic state, HTTP and DOM. No bridge is contacted.
const assert = require('assert'), fs = require('fs'), path = require('path'), vm = require('vm');
const source = fs.readFileSync(path.join(__dirname, '../avionmesh_pi/web/ui.js'), 'utf8');
const editor = source.slice(source.indexOf('/* ---------- keypad button editor'), source.indexOf('/* ---------- controller details'));
const baseline = {target:24575, observed_ms:200, target_generation:1};
const revision = 'a'.repeat(64);
function fixture(model=91, mode='ha') {
  const devices = [{avion_id:40001,product_type:model,name:'Hall control'}];
  const groups = [{group_id:300,name:'Hall',members:[40002],protected:false},
    {group_id:301,name:'Study',members:[40003],protected:false},
    {group_id:24575,name:'Private HA target',members:[],protected:true}];
  const current = {state:'configured',configured:true,mode,group:mode==='ha'?24575:300,transaction:'previous'};
  const view = {device_id:40001,product_id:model,control_configuration:current,
    actions:{configure_control:{enabled:true},setup_ha:{enabled:false},setup_native:{enabled:false}},
    observed_target:{baseline:{...baseline,target:current.group},valid_for_ms:15000}, slots:[]};
  const S = {groups,controllerViews:[view],radio:{label:null,queue:[]},status:{instance:'one',mesh_ready:true,
    mqtt_enabled:true,mqtt_ready:true,pending_transactions:0,jobs:{}},caps:{features:{keypad_edit:true}}};
  let now=1000, html='', handler=null;
  const elements = {'#modalRoot':{innerHTML:''}}, calls=[];
  const context=vm.createContext({S,Map,Set,Number,JSON,Math,Array,Object,Date:{now:()=>now},snapshotAt:1000,
    dev:id=>devices.find(d=>d.avion_id===id),feat:k=>S.caps.features[k],gName:id=>groups.find(g=>g.group_id===id)?.name||'Unknown',
    esc:String,pct:x=>Math.round(x*100/255),managementFresh:()=>true,groupBusy:false,
    lightingGroups:()=>groups.filter(g=>!g.protected),opsReviewButton:()=>'<button>Review Operations</button>',
    $:key=>elements[key]||null,renderSoon:()=>{},refreshSnapshot:async()=>{},toast:()=>{},
    closeModal:()=>{elements['#modalRoot'].innerHTML='';for(const key of Object.keys(elements))if(key!=='#modalRoot')delete elements[key];},
    modal:value=>{
      html=value; elements['#modalRoot'].innerHTML=value;
      for(const id of ['dimmerControlMode','dimmerControlGroup','dimmerControlGroupRow','dimmerControlStatus','dimmerControlPreview','dimmerControlApply'])elements['#'+id]={value:'',textContent:'',disabled:false,hidden:false};
      elements['#dimmerControlMode'].value=current.mode==='ha'?'ha':'native';
      elements['#dimmerControlGroup'].value=current.mode==='native'?String(current.group):'';
    },
    api:async(url,body)=>{
      calls.push({url,body:JSON.parse(JSON.stringify(body))});
      if(handler)return handler(url,body);
      if(url==='/api/dimmer/preview')return {target:40001,mode:body.mode,group:body.mode==='ha'?24574:body.group,
        revision,baseline:{...view.observed_target.baseline},valid_for_ms:15000};
      if(url==='/api/dimmer/configure')return {accepted:true,job_id:'job-one'};
      throw Error('Unexpected request');
    },operationNotStarted:detail=>detail?.operation_started===false});
  vm.runInContext(editor,context);
  return {S,view,devices,groups,calls,elements,run:code=>vm.runInContext(code,context),html:()=>html,
    now:value=>{now=value;},handler:fn=>{handler=fn;}};
}
(async()=>{
  let checks=0;const ok=title=>{checks++;console.log('PASS '+title);};
  for(const model of [81,91])for(const mode of ['native','ha']){
    const f=fixture(model,mode);
    assert(f.run('dimmerControlDetails(dev(40001),keypadView(40001))').includes('Change control mode or group'));
    f.run('openDimmerControl(40001)');
    assert(f.html().includes('Control mode')&&!f.html().includes('Private HA target'));
    if(model===81)assert(f.html().includes('Wake-Up'));
    f.elements['#dimmerControlMode'].value=mode==='ha'?'native':'ha';
    f.elements['#dimmerControlGroup'].value='301';f.run('changeDimmerControlChoice()');
    await f.run('previewDimmerControl()');
    assert(f.run('dimmerControlReviewValid(dimmerControlReview)'));
    f.run('confirmDimmerControl()');await new Promise(setImmediate);
    assert.equal(f.calls.length,2);
    const body=f.calls[1].body;
    assert.equal(f.calls[1].url,'/api/dimmer/configure');assert.equal(body.revision,revision);
    assert.equal(body.mode,mode==='ha'?'native':'ha');
    if(body.mode==='native')assert.equal(body.group,301);else assert(!Object.hasOwn(body,'group'));
    f.run('confirmDimmerControl()');assert.equal(f.calls.length,2,'closed review cannot resubmit');
  }
  ok('Both dimmer types change native or HA mode with one reviewed assignment');

  let f=fixture(91,'native');f.S.status.mqtt_ready=false;
  f.run('openDimmerControl(40001)');f.elements['#dimmerControlGroup'].value='301';
  await f.run('previewDimmerControl()');assert(f.run('dimmerControlReviewValid(dimmerControlReview)'));
  f.elements['#dimmerControlMode'].value='ha';f.run('changeDimmerControlChoice()');
  await f.run('previewDimmerControl()');assert.equal(f.calls.length,1);
  assert(f.elements['#dimmerControlStatus'].textContent.includes('Connect MQTT'));
  ok('Native group changes work with MQTT down; HA selection cannot dispatch');

  for(const mutation of ['expiry','baseline','restart','group','choice']){
    f=fixture();f.run('openDimmerControl(40001)');f.elements['#dimmerControlMode'].value='native';f.elements['#dimmerControlGroup'].value='301';
    await f.run('previewDimmerControl()');
    if(mutation==='expiry')f.now(17000);
    if(mutation==='baseline')f.view.observed_target.baseline.target_generation++;
    if(mutation==='restart')f.S.status.instance='two';
    if(mutation==='group')f.groups[1].protected=true;
    if(mutation==='choice')f.elements['#dimmerControlGroup'].value='300';
    f.run('confirmDimmerControl()');assert.equal(f.calls.length,1,mutation);
  }
  ok('Expired, changed, restarted or protected-destination reviews send nothing');

  f=fixture();f.run('openDimmerControl(40001)');f.elements['#dimmerControlMode'].value='native';f.elements['#dimmerControlGroup'].value='301';
  const select=f.elements['#dimmerControlGroup'];
  f.view.observed_target.valid_for_ms=8000;f.run('updateDimmerControlReview()');
  assert.strictEqual(f.elements['#dimmerControlGroup'],select);assert.equal(select.value,'301');
  assert.equal(f.elements['#dimmerControlMode'].value,'native');
  ok('Live observation updates preserve both form selections and DOM nodes');

  f=fixture();f.run('openDimmerControl(40001)');f.elements['#dimmerControlMode'].value='native';f.elements['#dimmerControlGroup'].value='301';
  f.handler((url,body)=>{
    if(url==='/api/dimmer/preview')return {target:40001,mode:'native',group:301,revision,baseline:{...baseline},valid_for_ms:15000};
    throw Error('Lost assignment response');
  });await f.run('previewDimmerControl()');f.run('confirmDimmerControl()');await new Promise(setImmediate);
  assert(f.run('accessoryRequests.get(40001).review'));
  assert.equal(f.run('accessoryAllowed(40001,"configure_control")'),false);
  f.run('openDimmerControl(40001)');assert.equal(f.calls.length,2);
  ok('A lost assignment response blocks another change and never retries');

  f=fixture();f.run('openDimmerControl(40001)');f.elements['#dimmerControlMode'].value='native';f.elements['#dimmerControlGroup'].value='301';
  await f.run('previewDimmerControl()');f.run('confirmDimmerControl()');await new Promise(setImmediate);
  f.S.status.jobs[40001]={id:'job-one',state:'completed',result:{transaction:'new-control',mode:'native',group:301,desired_behavior_observed:true,acceptance_required:true}};
  f.run('settleAccessoryRequests()');assert(f.run('accessoryRequests.get(40001).review'));
  f.view.control_configuration={state:'configured',configured:true,mode:'native',group:301,transaction:'foreign'};
  f.run('settleAccessoryRequests()');assert(f.run('accessoryRequests.get(40001).review'));
  f.view.control_configuration.transaction='new-control';f.run('settleAccessoryRequests()');
  assert.equal(f.run('accessoryRequests.get(40001).review'),false);
  ok('Only the matching accepted control receipt clears the review lock');

  for(const defect of ['none','foreign_job','wrong_mode','wrong_group','missing_receipt']){
    f=fixture();f.run('openDimmerControl(40001)');f.elements['#dimmerControlMode'].value='native';f.elements['#dimmerControlGroup'].value='301';
    await f.run('previewDimmerControl()');f.run('confirmDimmerControl()');await new Promise(setImmediate);
    const result={transaction:'timeout-receipt',mode:'native',group:301};
    if(defect==='wrong_mode')result.mode='ha';
    if(defect==='wrong_group')result.group=300;
    if(defect==='missing_receipt')delete result.transaction;
    f.S.status.jobs[40001]={id:defect==='foreign_job'?'unrelated':'job-one',state:'needs_review',result};
    f.run('settleAccessoryRequests()');
    if(defect==='none')assert.equal(f.run('accessoryRequests.get(40001).transaction'),'timeout-receipt');
    else assert.equal(f.run('accessoryRequests.get(40001).transaction'),undefined);
    f.view.control_configuration={state:'configured',configured:true,mode:'native',group:301,transaction:'unrelated-accepted'};
    f.run('settleAccessoryRequests()');assert.equal(f.run('accessoryAllowed(40001,"configure_control")'),false);
    f.view.control_configuration.transaction='timeout-receipt';f.run('settleAccessoryRequests()');
    assert.equal(f.run('accessoryAllowed(40001,"configure_control")'),defect==='none');
  }
  ok('A failed job keeps its exact receipt and only its accepted recovery unlocks control');

  for(const model of [126,127]){
    f=fixture(model,'native');f.view.assignment_read_back=true;f.view.revision='table-one';
    f.view.actions.edit_assignment={enabled:true};f.view.slots=[{slot:1,target:300,preset:255,status:'verified_mapping',assignment_mode:'native',editable:true,ha_available:true}];
    f.run('setKeypadChoice(40001,1,"mode","ha")');assert.equal(f.run('keypadChanges(40001)[0].mode'),'ha');
    f.S.status.mqtt_ready=false;assert.equal(f.run('keypadSaveAllowed(40001)'),false);
    f.run('setKeypadChoice(40001,1,"mode","native")');f.run('setKeypadChoice(40001,1,"group","301")');
    assert.equal(f.run('keypadSaveAllowed(40001)'),true);
    const html=f.run('keypadEditorHtml(dev(40001),keypadView(40001))');
    assert(html.includes('Direct group')&&html.includes('Home Assistant')&&!html.includes('value="24575"'));
    if(model===126)assert(html.includes('wake prompt'));
  }
  ok('Wired and battery keypads expose per-button modes with native control independent of MQTT');
  console.log(JSON.stringify({passed:true,checks}));
})().catch(error=>{console.error(error);process.exit(1);});
