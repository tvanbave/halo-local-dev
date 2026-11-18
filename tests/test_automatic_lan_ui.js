// Offline DOM/network stubs only. No browser, Bluetooth or HTTP listener.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),{execFileSync}=require('child_process');
const page=execFileSync('python3',['-c','from avionmesh_pi.ui import management_html; print(management_html(trusted_lan_no_auth=True))'],{encoding:'utf8'});
assert(!page.includes('id="token"'));
assert(page.includes('Trusted LAN access.'));
const source=page.split('<script>')[1].split('</script>')[0];
class Element {constructor(tag){this.tagName=tag;this.children=[];this.value='';this.textContent='';}append(...c){this.children.push(...c);}replaceChildren(...c){this.children=c;}setAttribute(n,v){this[n]=v;}set innerHTML(_){throw Error('Unsafe HTML');}}
const nodes=new Map(),get=id=>{assert.notEqual(id,'token','No token field may be read in LAN mode');if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);};
const requests=[];
const first='88000000000214000004000000000000',other='89000000000214000004000000000000';
const snapshot={status:{authentication_required:false,mesh_ready:true,mqtt_ready:false,
 bootstrap:{enabled:true,automatic:true,selected_address:'02:00:00:00:00:88',needs_first_claim:true},
 capabilities:{ordinary_claim:true},inventory_revision:1},inventory:{devices:[],groups:[]},bindings:{}};
const response=data=>({ok:true,json:async()=>data});
let failRefresh=false,holdDiscovery=false,holdSnapshot=false;const discoveries=[],snapshots=[];
const context=vm.createContext({document:{getElementById:get,createElement:t=>new Element(t)},
 window:{addEventListener(){}},AbortController,TextDecoder,Blob,URL,setTimeout:()=>0,confirm:()=>{throw Error('No first-device declaration should be needed');},
 fetch:async(url,options)=>{requests.push({url,options});assert(!('Authorization' in options.headers));
  if(url==='/api/snapshot'){
   if(holdSnapshot)return new Promise(resolve=>snapshots.push(resolve));
   if(failRefresh)return {ok:false,status:403,json:async()=>({error:'invalid_host'})};
   return response(snapshot);
  }
  if(url==='/api/discover'){
   if(holdDiscovery)return new Promise(resolve=>discoveries.push(resolve));
   return response({devices:[{uuid:first},{uuid:other}],truncated:false});
  }
  if(url==='/api/claim')return response({device_id:40001});
  if(url==='/api/events')return {ok:true,body:{getReader:()=>({read:async()=>({done:true})})}};
  throw Error('Unexpected endpoint '+url);
 }});
(async()=>{
 await vm.runInContext(source,context);
 assert.deepEqual(requests.map(r=>r.url),['/api/snapshot'],'Only status autoloads');
 assert.equal(get('bootstrapPanel').hidden,true);
 assert.equal(get('bootstrapScan').disabled,true);
 assert(get('connection').textContent.includes('Bridge connected'));
 assert.equal(vm.runInContext(`firstDeviceMatches('${first}')`,context),true);
 assert.equal(vm.runInContext(`firstDeviceMatches('${other}')`,context),true);
 await get('bootstrapScan').onclick();
 assert.equal(requests.length,1,'Hidden legacy scan cannot trigger a request');
 await get('refresh').onclick();await get('refresh').onclick();
 assert(requests.every(r=>r.url==='/api/snapshot'),'Refresh never scans or claims');
 await get('discover').onclick();
 assert.equal(get('candidates').children.length,2);
 const cards=get('candidates').children;
 const claimButtons=cards.map(c=>c.children.find(n=>n.tagName==='button'));
 assert(claimButtons.every(b=>b.disabled===false),'Claim is not gated by an endpoint selector');
 assert(!requests.some(r=>r.url==='/api/claim'),'Discovery must not claim anything');
 cards[1].children.find(n=>n.tagName==='input').value='Spare RL4';
 cards[1].children.find(n=>n.tagName==='select').value='93';
 await claimButtons[1].onclick();
 const claims=requests.filter(r=>r.url==='/api/claim');assert.equal(claims.length,1);
 assert.equal(JSON.parse(claims[0].options.body).uuid,other);
 assert.equal(claims[0].options.headers['Content-Type'],'application/json');
 holdDiscovery=true;
 const oldDiscovery=vm.runInContext('discover()',context);
 await get('refresh').onclick();
 discoveries.shift()(response({devices:[{uuid:first}]}));await oldDiscovery;
 assert.equal(get('candidates').children.length,2,'Refresh supersedes old discovery');
 holdSnapshot=true;
 const oldRefresh=vm.runInContext('refresh()',context);holdSnapshot=false;
 await get('refresh').onclick();
 snapshots.shift()({ok:false,status:403,json:async()=>({error:'old_error'})});await oldRefresh;
 assert(get('connection').textContent.includes('Bridge connected'));
 await vm.runInContext('liveUpdates()',context);
 assert(get('connection').textContent.includes('Status stale'));
 failRefresh=true;await get('refresh').onclick();
 assert(get('message').textContent.includes('This address is not allowed by the bridge'));
 assert(get('connection').textContent.includes('refresh failed'));
 failRefresh=false;await get('refresh').onclick();
 assert(get('connection').textContent.includes('Bridge connected'));
 assert(!requests.some(r=>r.url.startsWith('/api/bootstrap/')));
 assert(!/localStorage|sessionStorage|document\.cookie/.test(source));
 console.log(JSON.stringify({passed:true,checks:['token_free_autoload','no_auth_headers','automatic_no_selector','explicit_discover_and_claim','no_mac_gate','stale_responses_ignored','no_credential_storage','refresh_failure_recovery']}));
})().catch(error=>{console.error(error);process.exitCode=1;});
