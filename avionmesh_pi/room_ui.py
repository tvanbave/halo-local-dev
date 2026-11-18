"""Management-page room fragment. Inject once, replacing the old renderGroups."""
ROOM_SCRIPT = r"""
const roomSelections=new Map(),roomOutcomes=new Map();let roomBusy=false;
function roomEnabled(){return status.capabilities.verified_group_membership===true&&!status.preview_only&&!status.management_stale&&(typeof managementCurrent!=='function'||managementCurrent());}
function roomStatusText(row){const labels={verified:'Membership verified',not_applied:'Readback confirms the requested change was not applied',needs_readback:'Programming uncertain; read-only recovery required',not_verified:'Membership could not be verified',not_attempted:'Not attempted',verified_publication_pending:'Membership verified; publication needs review'};return labels[row.status]||'Review required';}
function renderGroups(){
 $('groups').replaceChildren();
 for(const g of inv.groups){
  const c=node('div');c.className='card';
  c.append(node('strong',g.name),node('small','Group '+g.group_id+(g.protected?' · reserved HA destination':' · '+g.members.length+' recorded members')));
  if(g.protected||g.group_id===24566){$('groups').append(c);continue;}
  c.append(node('small','Recorded members are metadata, not a fresh physical membership check.'));
  if(g.members.length>0&&g.members.every(m=>inv.devices.some(d=>d.device_id===m&&[90,93,94,97,134,137,162,167].includes(d.product_id))))controls(c,g.group_id,g.members.every(m=>inv.devices.some(d=>d.device_id===m&&[93,134,137,162].includes(d.product_id))),!g.members.some(m=>inv.devices.some(d=>d.device_id===m&&d.product_id===167)));
  metadataControls(c,'group',g,g.group_id);
  if(!g.members.length)c.append(button('Forget empty metadata group',()=>metadata('forget_empty_group',{target:g.group_id}),roomBusy));
  if(!roomEnabled())c.append(node('p','Physical room setup is unavailable until programming and physical readback are enabled after qualification. Creating a name does not add lights.'));
  const chosen=roomSelections.get(g.group_id)||new Set();roomSelections.set(g.group_id,chosen);
  const eligible=inv.devices.filter(d=>[93,134].includes(d.product_id));
  for(const target of [...chosen])if(!eligible.some(d=>d.device_id===target))chosen.delete(target);
  const list=node('fieldset');list.append(node('legend','Select up to 32 lights explicitly'));
  const add=button('Add selected lights',()=>applyRoom(g.group_id,true));
  const remove=button('Remove selected lights',()=>applyRoom(g.group_id,false));
  function enable(){add.disabled=remove.disabled=!roomEnabled()||roomBusy||!chosen.size||chosen.size>32||!status.mesh_ready||status.pending_transactions>0;}
  for(const device of eligible){const label=node('label'),check=node('input');check.type='checkbox';check.checked=chosen.has(device.device_id);check.disabled=!roomEnabled()||roomBusy;check.onchange=()=>{if(check.disabled)return;if(check.checked)chosen.add(device.device_id);else chosen.delete(device.device_id);enable();};label.append(check,node('span',device.name+' · '+device.device_id+(g.members.includes(device.device_id)?' · recorded member':'')));list.append(label);}
  if(!eligible.length)list.append(node('small','No eligible RL4 or A19 lights are available.'));
  enable();c.append(list,add,remove);
  const outcome=roomOutcomes.get(g.group_id);
  if(outcome){const report=node('div');report.setAttribute('role','status');report.append(node('p',outcome.message));for(const row of outcome.rows||[]){report.append(node('p',(row.name||row.target)+': '+roomStatusText(row)));if(row.publication_pending)report.append(node('small','Metadata publication needs review; physical readback outcome is shown above.'));if(row.transaction)report.append(node('small','Transaction '+row.transaction+'. Open Operations and recovery for read-only reconciliation. No programming is retried automatically.'));}c.append(report);}
  $('groups').append(c);
 }
}
async function applyRoom(group,member){
 if(roomBusy||!roomEnabled()||!status.mesh_ready||status.pending_transactions>0)return;
 const targets=[...(roomSelections.get(group)||[])];if(!targets.length)return;if(targets.length>32)throw Error('Select no more than 32 lights for one reviewed operation.');
 const room=inv.groups.find(g=>g.group_id===group&&!g.protected&&g.group_id!==24566);if(!room)return;
 if(!targets.every(t=>inv.devices.some(d=>d.device_id===t&&[93,134].includes(d.product_id))))throw Error('Refresh and select eligible lights again.');
 const names=targets.map(t=>inv.devices.find(d=>d.device_id===t).name).join(', ');
 if(!confirm((member?'Add ':'Remove ')+names+(member?' to ':' from ')+room.name+'? Each light will be read back. Setup stops at the first problem and never resumes automatically.'))return;
 const epoch=connectionEpoch,token=currentToken();roomBusy=true;roomSelections.delete(group);
 roomOutcomes.set(group,{message:'Checking and programming selected lights. Waiting for verified readback.',rows:[]});renderGroups();
 try{const result=await api('rooms/membership',{targets,group,member});if(epoch!==connectionEpoch||token!==currentToken())return;roomOutcomes.set(group,result);return result.message;}
 catch(error){if(epoch===connectionEpoch&&token===currentToken())roomOutcomes.set(group,{message:'The response was interrupted. The outcome is unknown. Refresh and inspect Operations and recovery before selecting any lights again.',rows:[]});throw error;}
 finally{roomBusy=false;renderGroups();}
}
"""
