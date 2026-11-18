// Exercise the shipped control/API functions with synthetic fetch and DOM only.
const assert = require('assert'), fs = require('fs'), path = require('path'), vm = require('vm');
const source = fs.readFileSync(path.join(__dirname, '../avionmesh_pi/web/ui.js'), 'utf8');
const api = source.slice(source.indexOf('const ERRORS ='), source.indexOf('const getJson ='));
const controls = source.slice(source.indexOf('function lightValues('), source.indexOf('/* ---------- actions'));

function fixture(responses) {
  const calls = [], notices = [], entries = [], waits = [];
  const state = {pending:{}, radio:{label:null,queue:[]}, devices:[{avion_id:40002,brightness:51}]};
  const context = vm.createContext({S:state, toast:m=>notices.push(m),
    log:(kind,title,detail)=>entries.push({kind,title,detail}), renderSoon:()=>{}, renderBusy:()=>{},
    sleep:async ms=>waits.push(ms), fetch:async(url, options)=>{
      calls.push({url,body:JSON.parse(options.body)});
      const response = responses.shift();
      assert(response, 'Unexpected retry');
      if (response instanceof Error) throw response;
      return {ok:response.status<400,status:response.status,json:async()=>response.body};
    }});
  vm.runInContext(api + controls, context);
  return {calls, notices, entries, waits, state,
    control:()=>vm.runInContext("control(40002, {brightness:128}, 'Set Theatre to 50%')",context)};
}

(async()=>{
  let passed = 0;
  for (const [body, expected, warning] of [
    [{sent:true,readbacks:[],confirmed:false}, 'no light replied', true],
    [{sent:true,readbacks:[{device_id:40002,brightness:128}],confirmed:false}, '1 light replied, but the full change is not confirmed', true],
    [{sent:true,readbacks:[{device_id:40002,brightness:64},{device_id:40003,brightness:128}],confirmed:false}, '2 lights replied, but the full change is not confirmed', true],
    [{sent:true,readbacks:[{device_id:40002,brightness:128}],confirmed:true}, 'Confirmed; 1 light replied.', false],
    [{sent:true,readbacks:[{device_id:40002,brightness:128},{device_id:40003,brightness:128}],confirmed:true}, 'Confirmed; 2 lights replied.', false],
    [{sent:true,readbacks:[],confirmed:true}, 'no light replied', true],
    [{sent:true,readbacks:2,confirmed:true}, 'no light replied', true],
  ]) {
    const f=fixture([{status:200,body}]);
    await f.control();
    assert.equal(f.calls.length,1);
    assert.equal(f.notices.length,warning ? 1 : 0);
    assert(f.entries[0].detail.includes(expected),f.entries[0].detail);
    assert(!f.entries[0].detail.includes('[object Object]'));
    assert.equal(f.state.devices[0].brightness,51,'A command result must not replace reported state');
    assert.equal(f.state.pending[40002],undefined);
    passed++;
  }
  for (const body of [
    {error:'operation_unavailable_or_uncertain'},
    {error:'mesh_busy'},
    {error:'mesh_busy',operation_started:false,requires_review:false,retry_allowed:false},
    {error:'mesh_busy',operation_started:false,requires_review:false},
    {error:'mesh_busy',operation_started:false,requires_review:true},
    {error:'session_changed',operation_started:false,requires_review:false},
    {error:'unexpected',operation_started:false,requires_review:false},
  ]) {
    const f=fixture([{status:409,body}]);
    await assert.rejects(f.control());
    assert.equal(f.calls.length,1,'Only an explicit safe busy refusal may be retried');
    assert.equal(f.waits.length,0);
    assert.equal(f.state.pending[40002],undefined);
    passed++;
  }
  for (const error of ['background_read_busy','mesh_busy']) {
    const f=fixture([
      {status:409,body:{error,operation_started:false,requires_review:false,retry_allowed:true}},
      {status:200,body:{sent:true,readbacks:[{device_id:40002,brightness:128}],confirmed:true}},
    ]);
    await f.control();
    assert.equal(f.calls.length,2);
    assert.deepEqual(f.calls[0],f.calls[1]);
    assert.deepEqual(f.waits,[800]);
    passed++;
  }
  const lost=fixture([new Error('Connection lost after dispatch')]);
  await assert.rejects(lost.control());
  assert.equal(lost.calls.length,1);
  assert.equal(lost.waits.length,0);
  passed++;
  console.log(`${passed} control feedback and dispatch checks passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
