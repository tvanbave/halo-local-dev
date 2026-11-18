// Synthetic DOM only: never contacts a bridge, device or broker.
const assert=require('assert'),vm=require('vm'),{execFileSync}=require('child_process');
const source=execFileSync('python3',['-c','from avionmesh_pi.room_ui import ROOM_SCRIPT; print(ROOM_SCRIPT)'],{encoding:'utf8'});
class Element{constructor(tag,text){this.tagName=tag;this.textContent=text||'';this.children=[];}append(...nodes){this.children.push(...nodes);}replaceChildren(...nodes){this.children=nodes;}setAttribute(k,v){this[k]=v;}}
const root=new Element('div'),calls=[],confirmations=[];
let finish;
const ctx=vm.createContext({Map,Set,console,$:()=>root,node:(t,s)=>new Element(t,s),
 button:(s,fn,disabled=false)=>Object.assign(new Element('button',s),{onclick:fn,disabled}),
 controls(){},metadataControls(){},metadata(){throw Error('No implicit metadata mutation');},
 currentToken:()=>'',connectionEpoch:1,
 status:{capabilities:{verified_group_membership:false},mesh_ready:true,pending_transactions:0},
 inv:{groups:[{group_id:291,name:'Room',members:[],protected:false},{group_id:24566,name:'Reserved',members:[],protected:true}],devices:[{device_id:40002,name:'One',product_id:93},{device_id:40003,name:'Two',product_id:93},{device_id:40004,name:'Other model',product_id:94}]},
 confirm:s=>{confirmations.push(s);return true;},
 api:async(path,body)=>{calls.push({path,body});return new Promise(resolve=>{finish=resolve;});}});
function run(s){return vm.runInContext(s,ctx);}
function descendants(n=root){return [n,...n.children.flatMap(descendants)];}
function buttons(){return descendants().filter(n=>n.tagName==='button'&&n.textContent==='Add selected lights');}
(async()=>{
 run(source);run('renderGroups()');assert.equal(calls.length,0);assert(buttons().every(b=>b.disabled));
 assert(descendants().some(n=>n.textContent.includes('Creating a name does not add lights')));
 await run('applyRoom(291,true)');assert.equal(calls.length,0);
 run('status.capabilities.verified_group_membership=true;renderGroups()');
 assert(buttons().every(b=>b.disabled),'No default selection');
 await run('applyRoom(291,true)');assert.equal(confirmations.length,0);
 let check=descendants().filter(n=>n.tagName==='input')[1];check.checked=true;check.onchange();
 assert.equal(buttons()[0].disabled,false);run('renderGroups()');
 assert.equal(descendants().filter(n=>n.tagName==='input')[1].checked,true,'Selection survives refresh');
 const pending=run('applyRoom(291,true)');await run('applyRoom(291,true)');
 assert.equal(calls.length,1,'Duplicate click blocked');assert.deepEqual(Array.from(calls[0].body.targets),[40003]);
 assert.equal(calls[0].path,'rooms/membership');assert(confirmations[0].includes('Two to Room'));
 assert(buttons().every(b=>b.disabled));
 finish({complete:false,message:'Stopped for review.',rows:[{target:40003,name:'Two',status:'needs_readback',transaction:'synthetic-1'}]});await pending;
 assert(descendants().some(n=>n.textContent.includes('read-only reconciliation')));
 assert(buttons().every(b=>b.disabled),'Finished operation clears selection; no implicit resume');
 await run('applyRoom(291,true)');assert.equal(calls.length,1);
 run('roomSelections.set(291,new Set([40004]))');await assert.rejects(run('applyRoom(291,true)'),/eligible/);assert.equal(calls.length,1);
 run('roomSelections.set(24566,new Set([40002]))');await run('applyRoom(24566,true)');assert.equal(calls.length,1);
 run('roomSelections.set(291,new Set([40002]));status.preview_only=true');await run('applyRoom(291,true)');assert.equal(calls.length,1);
 console.log('Room UI synthetic checks passed: disabled gates, explicit selection, preservation, exact target, duplicate guard, stop/recovery, unsupported and protected targets, preview.');
})().catch(e=>{console.error(e);process.exitCode=1;});
