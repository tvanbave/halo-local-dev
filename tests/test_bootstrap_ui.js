const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=require('child_process').execFileSync('python3',['-c','from avionmesh_pi.ui import HTML; print(HTML)'],{encoding:'utf8'}).split('<script>')[1].split('</script>')[0];
class Element {constructor(tag){this.tagName=tag;this.children=[];this.value='';this.textContent='';}append(...c){this.children.push(...c);}replaceChildren(...c){this.children=c;}setAttribute(n,v){this[n]=v;}set innerHTML(_){throw Error('Unsafe HTML');}}
const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);};
const scans=[],posts=[];let confirm=false;
const address='02:00:00:00:00:88';
const snapshot={status:{bootstrap:{enabled:true,selected_address:address,needs_first_claim:true},capabilities:{},inventory_revision:1},inventory:{devices:[],groups:[]},bindings:{}};
const context=vm.createContext({document:{getElementById:get,createElement:t=>new Element(t)},window:{addEventListener(){}},AbortController,TextDecoder,Blob,URL,setTimeout:()=>0,confirm:()=>confirm,
 fetch:async(url,options)=>{posts.push({url,body:options.body});if(url==='/api/bootstrap/scan')return new Promise(resolve=>scans.push(resolve));return {ok:true,json:async()=>url==='/api/snapshot'?snapshot:{selected:true,association_started:false}};}});
const reply={ok:true,json:async()=>({candidates:[{address,rssi:-60}]})};
(async()=>{
 vm.runInContext(source,context);get('token').value='A';
 const scan=vm.runInContext('bootstrapScan()',context);scans.shift()(reply);await scan;
 const choose=get('bootstrapCandidates').children[0].children.find(n=>n.tagName==='button');
 await choose.onclick();assert(!posts.some(p=>p.url==='/api/bootstrap/select'));
 confirm=true;await choose.onclick();assert.deepEqual(JSON.parse(posts.find(p=>p.url==='/api/bootstrap/select').body),{address,owner_reset:true});
 assert.equal(get('bootstrapScan').disabled,true);
 assert.equal(vm.runInContext("firstDeviceMatches('88000000000214000004000000000000')",context),true);
 assert.equal(vm.runInContext("firstDeviceMatches('89000000000214000004000000000000')",context),false);
 const stale=vm.runInContext('bootstrapScan()',context);get('token').oninput();scans.shift()(reply);await stale;
 assert.equal(get('bootstrapCandidates').children.length,0);
 assert(!posts.some(p=>p.url==='/api/claim'));
 console.log(JSON.stringify({passed:true,checks:['explicit_reset_declaration','no_automatic_claim','selected_endpoint_locked','first_uuid_matches_selected_mac','stale_scan_ignored']}));
})().catch(e=>{console.error(e);process.exit(1);});
