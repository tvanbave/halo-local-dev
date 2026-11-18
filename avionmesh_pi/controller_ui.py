"""Text-only contextual controller UI; injected by the management page owner."""
CONTROLLER_WORKFLOW_JS = r'''
const controllerViews=new Map(),controllerOutcomes=new Map(),controllerAwaiting=new Map(),controllerPendingActions=new Map();
let controllerBusy=false;
const controllerNativeEvidence=new Map(),controllerQueues=new Map(),controllerQueueDrafts=new Map();
let controllerOpenQueue=null;
const controllerReasonLabels={read_only_preview:'Read-only preview.',mesh_offline:'Bridge offline.',operation_in_progress:'Another operation is running.',resolve_pending_operations:'Review unfinished operations first.',assignment_readback_unsupported:'Stored assignment readback is unsupported for this model.',read_assignments_first:'Read assignments first.',model_editor_disabled:'Programming is not enabled for this model.',model_setup_disabled:'Automatic HA setup is not enabled for this model.',authoritative_inventory_required:'A complete authoritative inventory is required.',fresh_retained_claim_required:'Setup requires a fresh retained claim and cannot be repeated.',candidate_requires_explicit_target_and_fresh_baseline:'Target programming requires a separately reviewed destination and fresh native observation.',assignment_clear_unsupported:'Stored assignment clear is unsupported.',fresh_native_baseline_required:'Observe a fresh native brightness command before reviewing a target change.',protected_native_source:'This managed HA target must use its protected lifecycle.',ordinary_destination_required:'Add an ordinary native group different from the observed target first.'};
function controllerReason(reason){return controllerReasonLabels[reason]||String(reason||'Unavailable').replaceAll('_',' ');}
function controllerNotStarted(job){return job?.operation_started===false&&job.requires_review===false&&['background_read_busy','mesh_busy','mesh_offline','session_changed','inventory_changed'].includes(job.error);}
function controllerAllowed(id,action){const v=controllerViews.get(id);return (typeof managementCurrent!=='function'||managementCurrent())&&!controllerBusy&&!controllerAwaiting.has(id)&&!!v?.actions?.[action]?.enabled&&status.preview_only!==true&&status.management_stale!==true&&(action==='local_input'||status.mesh_ready===true);}
function controllerRedraw(){renderControllerWorkflows([...controllerViews.values()]);}
async function controllerRequest(id,action,path,body,question,options={}){
 if(!controllerAllowed(id,action))return;
 if(question&&!confirm(question))return;
 if(!controllerAllowed(id,action))return;
 if(options.check)options.check();
 const epoch=connectionEpoch,token=currentToken();controllerBusy=true;controllerAwaiting.set(id,null);controllerPendingActions.set(id,action);controllerOutcomes.set(id,'Request in progress. Do not repeat it.');controllerRedraw();
 let result,refreshed=false;
 try{
  // Only a failed POST makes the operation outcome uncertain. A failed metadata
  // refresh below must not erase an accepted job ID or verified write result.
  try{result=await api(path,body);}
  catch(error){
   if(epoch===connectionEpoch&&token===currentToken()){
    if(controllerNotStarted(error.details)){controllerAwaiting.delete(id);controllerPendingActions.delete(id);controllerOutcomes.set(id,error.details.message||error.message||'The operation was not started. Refresh and review before trying again.');}
    else if(action==='read_assignments'&&[400,401,403,409].includes(error.status)){controllerAwaiting.delete(id);controllerPendingActions.delete(id);controllerOutcomes.set(id,'Assignment read was rejected before acceptance. '+error.message+'. Refresh before explicitly trying again.');}
    else{controllerAwaiting.set(id,null);controllerOutcomes.set(id,'Outcome not confirmed: '+error.message+'. Review Operations and recovery before retrying.');}
   }
   throw error;
  }
  if(epoch!==connectionEpoch||token!==currentToken())return;
  if(result.accepted===true&&result.completed===false){controllerAwaiting.set(id,result.job_id||null);controllerOutcomes.set(id,'Request accepted, not yet verified. Watch progress below; do not repeat it.');}
  else if(result.verified===false||result.requires_review||result.acceptance_required){controllerOutcomes.set(id,'Needs review. No write was retried automatically.');}
  else{controllerAwaiting.delete(id);controllerPendingActions.delete(id);controllerOutcomes.set(id,result.verified===true?'Selected change verified by full assignment readback.':'Request finished. Refresh evidence before making another change.');}
  try{
   const fresh=await api('snapshot');
   if(epoch===connectionEpoch&&token===currentToken()){acceptSnapshot(fresh,true);refreshed=true;}
  }catch(error){if(epoch===connectionEpoch&&token===currentToken())controllerOutcomes.set(id,controllerOutcomes.get(id)+' Could not refresh metadata. The recorded operation result is unchanged; refresh before further review.');}
  if(epoch!==connectionEpoch||token!==currentToken())return;
  if(options.onResult)return options.onResult(result,refreshed);
  return 'Controller request recorded. Review its current status below.';
 }finally{controllerBusy=false;if(epoch===connectionEpoch&&token===currentToken())controllerRedraw();}
}
function controllerOrdinaryGroups(){return inv.groups.filter(g=>!g.protected&&Number.isInteger(g.group_id)&&g.group_id>=256&&g.group_id<=24575);}
function controllerBaselineKey(value){return value?JSON.stringify([value.target,value.observed_ms,value.target_generation]):'';}
function controllerNativeFresh(id,baseline){
 const evidence=controllerNativeEvidence.get(id),current=controllerViews.get(id)?.observed_target;
 return !!evidence&&!!baseline&&evidence.instance===status.instance&&evidence.epoch===connectionEpoch&&evidence.token===currentToken()&&Date.now()>=evidence.seen&&Date.now()<evidence.deadline&&controllerBaselineKey(current?.baseline)===controllerBaselineKey(baseline);
}
function openControllerNativeEditor(id){
 const v=controllerViews.get(id),baseline=v?.observed_target?.baseline?{...v.observed_target.baseline}:null;
 if(!controllerAllowed(id,'assign_native_target'))return;
 if(!controllerNativeFresh(id,baseline))throw Error('Native baseline expired. Observe a fresh command and refresh before reviewing a target.');
 controllerOpenQueue=null;
 const epoch=connectionEpoch,token=currentToken(),instance=status.instance,panel=$('proposal');
 panel.replaceChildren(node('h3','Review native target for '+(inv.devices.find(d=>d.device_id===id)?.name||id)));
 const destination=select([['__choose__','Choose an ordinary native group'],...controllerOrdinaryGroups().filter(g=>g.group_id!==baseline.target).map(g=>[g.group_id,g.name+' (#'+g.group_id+')'])]);
 const label=node('label','New native destination');label.append(destination);
 const review=node('div');
 panel.append(label,node('p','Observed original target: '+baseline.target+'. This is a recent command, not stored assignment readback or a complete assignment backup.'),node('p',v.product_id===81?'After submission, model 81 waits for a new wake report within the original 15-second baseline window. Its 250 ms dispatch budget is an unqualified candidate policy.':'The original observation expires after 15 seconds. If it changes or expires, observe again and start a new review.'),node('p','This changes a native target. It does not read or clear assignments or run automatic HA setup. Unknown outcomes will not be retried.'));
 const check=()=>{
  if(epoch!==connectionEpoch||token!==currentToken()||instance!==status.instance||!controllerAllowed(id,'assign_native_target')||!controllerNativeFresh(id,baseline))throw Error('Native baseline changed or expired. Observe a new command and reopen the review.');
  if(inv.groups.some(g=>g.protected&&g.group_id===baseline.target))throw Error('Protected native assignment requires its managed lifecycle.');
 };
 panel.append(button('Preview native target change',()=>{
  check();const group=Number(destination.value),choice=controllerOrdinaryGroups().find(g=>g.group_id===group);
  if(!choice||group===baseline.target)throw Error('Choose a different ordinary native group.');
  const selected=destination.value,question='Change controller '+id+' from observed target '+baseline.target+' to '+choice.name+' (#'+group+')? This writes its native destination. Observation is not stored assignment readback. No automatic retry or rollback.';
  review.replaceChildren(node('p',question),button('Apply reviewed native target',()=>controllerRequest(id,'assign_native_target','dimmer/assign',{target:id,group,baseline},question,{check:()=>{check();if(destination.value!==selected||!controllerOrdinaryGroups().some(g=>g.group_id===group))throw Error('Destination changed. Preview again.');}})));
  return 'Native target review ready. No physical assignment has changed.';
 }));panel.append(review,button('Close review',()=>panel.replaceChildren()));
}
function controllerEventText(v){
 const e=v.last_event;if(!e)return 'No controller event observed in this bridge session.';
 const parts=[e.input_mode==='local_level_reports'?'Last local level report: ':'Last observed command: '];
 parts.push(({on:'On',off:'Off',brightness:'Brightness',raise:'Raise',lower:'Lower',mesh_command:'Mesh command'})[e.event_type]||String(e.event_type));
 if(Number.isInteger(e.brightness))parts.push(' · level '+e.brightness+'/255');
 if(Number.isInteger(e.target))parts.push(' · target '+e.target);
 if(Number.isInteger(e.button)&&e.button_mapping==='verified_table')parts.push(' · Button '+e.button+(e.activation?' activated':' (verified target mapping)'));
 else if(Number.isInteger(e.button)&&e.button_mapping==='cached_table')parts.push(' · Button '+e.button+' (cached mapping; no fresh assignment readback)');
 else if([126,127].includes(v.product_id))parts.push(' · button identity unknown');
 parts.push(' · '+Math.floor(e.age_ms/1000)+' seconds ago. This is observed input, not proof of Home Assistant receipt.');
 return parts.join('');
}
function controllerAction(card,v,name,label,path,body,question){
 card.append(button(label,()=>controllerRequest(v.device_id,name,path,body,question),!controllerAllowed(v.device_id,name)));
 if(!v.actions[name]?.enabled)card.append(node('small',controllerReason(v.actions[name]?.reason)));
}
function openControllerEditor(id,slot){
 const v=controllerViews.get(id),row=v?.slots.find(s=>s.slot===slot);
 if(!controllerAllowed(id,'edit_assignment')||!row?.editable||inv.groups.some(g=>g.protected&&g.group_id===row.target))return;
 controllerOpenQueue=null;
 const panel=$('proposal');panel.replaceChildren(node('h3','Edit button '+slot+' on controller '+id));
 const destination=select([['__choose__','Choose a destination or explicitly clear'],['','Unassigned (clear this slot)'],...controllerOrdinaryGroups().map(g=>[g.group_id,g.name])]);
 destination.value=row.target===65535?'':String(row.target);
 // A current target absent from editable group inventory must not silently become Clear.
 if(row.target!==65535&&!controllerOrdinaryGroups().some(g=>g.group_id===row.target))destination.value='__choose__';
 const level=node('input');level.type='number';level.min=1;level.max=255;level.value=row.preset||255;
 const groupLabel=node('label','Desired native group (Unassigned clears this slot)');groupLabel.append(destination);
 const levelLabel=node('label','Preset brightness, 1 to 255');levelLabel.append(level);
 panel.append(groupLabel,levelLabel,node('p','Review the selected destination and preset. HA setup preserves occupied slots; this editor changes the selected slot only.'));
 const revision=v.revision,epoch=connectionEpoch,token=currentToken(),instance=status.instance;
 const check=()=>{if(epoch!==connectionEpoch||token!==currentToken()||instance!==status.instance||!controllerAllowed(id,'edit_assignment')||controllerViews.get(id)?.revision!==revision)throw Error('Assignments or connection changed. Read them again before editing.');};
 panel.append(button('Preview selected change',async()=>{
  check();
  if(destination.value==='__choose__')throw Error('Choose a destination or explicitly select Clear. The existing assignment has not changed.');
  if(destination.value&&(!Number.isInteger(Number(level.value))||Number(level.value)<1||Number(level.value)>255))throw Error('Preset must be an integer from 1 to 255.');
  const request={target:id,slot,mode:destination.value?'native':'unassigned',revision};
  if(destination.value){request.group=Number(destination.value);request.brightness=Number(level.value);}
  const selected=destination.value,preset=level.value;
  const proposal=await api('keypad/preview',request);check();
  const text=request.mode==='unassigned'?'Clear button '+slot+' on controller '+id+'?':'Set button '+slot+' on controller '+id+' to group '+request.group+' at preset '+request.brightness+'/255?';
  const review=node('div');review.append(node('p',text),button('Apply reviewed change',async()=>{
   check();
   return controllerRequest(id,'edit_assignment','keypad-configure',request,text+' This writes the physical assignment. Unknown outcomes will not be retried.',{check:()=>{check();if(destination.value!==selected||level.value!==preset)throw Error('Draft changed. Preview again.');controllerSlotRequest(id,controllerViews.get(id).slots.find(r=>r.slot===slot),{destination:selected,level:preset},revision);}});
  },proposal.executable!==true||proposal.changed!==true));panel.append(review);
  return 'Preview ready. No physical assignment has changed.';
 }));
}
// A bounded operator review queue, not a transport batch or a recovery protocol.
// Every slot uses the existing verified one-slot API and an individual confirmation.
function controllerQueueCurrent(id,q){
 return q.epoch===connectionEpoch&&q.token===currentToken()&&q.instance===status.instance&&controllerViews.get(id)?.revision===q.revision;
}
function controllerQueueCheck(id,q){
 if(!controllerQueueCurrent(id,q)||!controllerAllowed(id,'edit_assignment'))throw Error('Queue evidence changed, expired or disconnected. Stop and read assignments again.');
 if(q.stopped||q.running||controllerQueues.get(id)!==q)throw Error('This review queue cannot continue.');
}
function controllerSlotRequest(id,row,draft,revision){
 if(!row?.editable||inv.groups.some(g=>g.protected&&g.group_id===row.target))throw Error('This button is not editable through the ordinary native editor.');
 if(draft.destination==='__choose__')throw Error('Choose a destination or explicitly choose Clear for every selected button.');
 const request={target:id,slot:row.slot,mode:draft.destination===''?'unassigned':'native',revision};
 if(request.mode==='native'){
  request.group=Number(draft.destination);request.brightness=Number(draft.level);
  if(!controllerOrdinaryGroups().some(g=>g.group_id===request.group))throw Error('Choose an ordinary known native group.');
  if(!Number.isInteger(request.brightness)||request.brightness<1||request.brightness>255)throw Error('Preset must be an integer from 1 to 255.');
 }
 return request;
}
function controllerSlotLabel(entry){return 'Button '+entry.slot+': '+(entry.mode==='unassigned'?'clear to Unassigned':'group '+entry.group+' at preset '+entry.brightness+'/255');}
function openControllerQueue(id){
 const existing=controllerQueues.get(id);controllerOpenQueue=id;
 if(existing){renderControllerQueue(id);return;}
 if(!controllerAllowed(id,'edit_assignment')){controllerOpenQueue=null;return;}
 const v=controllerViews.get(id),panel=$('proposal'),epoch=connectionEpoch,token=currentToken(),instance=status.instance;
 panel.replaceChildren(node('h3','Plan button changes for '+id),node('p','Select up to four buttons. Each change gets its own preview and confirmation. After every verified write, the full table must match before the next review. Nothing is applied automatically.'));
 let drafts=controllerQueueDrafts.get(id);if(!drafts){drafts=new Map();controllerQueueDrafts.set(id,drafts);}
 for(const row of v.slots){
  if(!row.editable||inv.groups.some(g=>g.protected&&g.group_id===row.target))continue;
  let draft=drafts.get(row.slot);if(!draft){draft={selected:false,destination:row.target===65535?'':controllerOrdinaryGroups().some(g=>g.group_id===row.target)?String(row.target):'__choose__',level:String(row.preset||255)};drafts.set(row.slot,draft);}
  const selected=node('input');selected.type='checkbox';selected.checked=draft.selected;selected.onchange=()=>{draft.selected=selected.checked;};
  const label=node('label','Include button '+row.slot);label.append(selected);
  const destination=select([['__choose__','Choose a destination or explicitly clear'],['','Unassigned (clear this slot)'],...controllerOrdinaryGroups().map(g=>[g.group_id,g.name+' (#'+g.group_id+')'])]);destination.value=draft.destination;destination.onchange=()=>{draft.destination=destination.value;};
  const level=node('input');level.type='number';level.min=1;level.max=255;level.value=draft.level;level.oninput=()=>{draft.level=level.value;};
  const destinationLabel=node('label','Button '+row.slot+' destination');destinationLabel.append(destination);const levelLabel=node('label','Button '+row.slot+' exact preset, 1–255');levelLabel.append(level);
  panel.append(label,destinationLabel,levelLabel);
 }
 panel.append(button('Review selected button sequence',()=>{
  if(epoch!==connectionEpoch||token!==currentToken()||instance!==status.instance||!controllerAllowed(id,'edit_assignment')||controllerViews.get(id)?.revision!==v.revision)throw Error('Assignments or connection changed. Close this plan, read assignments and review your drafts again.');
  const chosen=[...drafts].filter(([,d])=>d.selected);
  if(!chosen.length||chosen.length>4)throw Error('Select between one and four distinct buttons.');
  const entries=chosen.map(([slot,draft])=>({...controllerSlotRequest(id,v.slots.find(r=>r.slot===slot),draft,v.revision),state:'pending'}));
  if(new Set(entries.map(e=>e.slot)).size!==entries.length)throw Error('Each button can appear only once.');
  const native=entries.filter(e=>e.mode==='native').map(e=>e.group);if(new Set(native).size!==native.length)throw Error('Two buttons cannot use the same target in a verified button mapping.');
  const q={epoch,token,instance,revision:v.revision,rows:v.slots.map(r=>({...r})),entries,running:false,stopped:false,preview:null};
  controllerQueues.set(id,q);renderControllerQueue(id);
  return 'Sequence prepared. Review and confirm each button individually; no write has been sent.';
 }),button('Close plan',()=>{controllerOpenQueue=null;panel.replaceChildren();}));
}
async function previewControllerQueue(id){
 const q=controllerQueues.get(id);if(!q)return;controllerQueueCheck(id,q);
 const entry=q.entries.find(e=>e.state==='pending');if(!entry)return;
 const request={target:id,slot:entry.slot,mode:entry.mode,revision:q.revision};if(entry.mode==='native'){request.group=entry.group;request.brightness=entry.brightness;}
 // Revalidate preserved drafts against the latest inventory, without changing them.
 controllerSlotRequest(id,controllerViews.get(id).slots.find(r=>r.slot===entry.slot),{destination:entry.mode==='native'?String(entry.group):'',level:entry.brightness},q.revision);
 q.preview=null;q.running=true;renderControllerQueue(id);
 try{
  const proposal=await api('keypad/preview',request);
  if(!controllerQueueCurrent(id,q)||controllerQueues.get(id)!==q||!controllerAllowed(id,'edit_assignment'))throw Error('Queue evidence changed during preview.');
  if(proposal.executable!==true||proposal.revision!==q.revision||proposal.slot!==entry.slot||typeof proposal.desired!=='string')throw Error('The selected change is not executable against this full-table revision.');
  if(proposal.changed===false){entry.state='unchanged';q.preview=null;return 'Button '+entry.slot+' already matches. No write sent. Review the next button explicitly.';}
  if(proposal.changed!==true)throw Error('The selected change has no definite preview outcome.');
  q.preview={entry,request,desired:proposal.desired};
  return 'Preview ready. Apply only this reviewed button when ready.';
 }catch(error){q.stopped=true;q.reason='Preview stopped: '+error.message+' No further button will be sent.';throw error;}
 finally{q.running=false;if(controllerOpenQueue===id)renderControllerQueue(id);}
}
async function applyControllerQueue(id){
 const q=controllerQueues.get(id);if(!q?.preview)return;controllerQueueCheck(id,q);
 const preview=q.preview,entry=preview.entry,question=controllerSlotLabel(entry)+' on controller '+id+'? This writes only this button. Remaining buttons require separate review and confirmation.';
 const check=()=>{
  controllerQueueCheck(id,q);
  if(q.preview!==preview)throw Error('The preview changed. Review again.');
  controllerSlotRequest(id,controllerViews.get(id).slots.find(r=>r.slot===entry.slot),{destination:entry.mode==='native'?String(entry.group):'',level:entry.brightness},q.revision);
  q.running=true;entry.state='submitting';
 };
 try{return await controllerRequest(id,'edit_assignment','keypad-configure',preview.request,question,{check,onResult:(result,refreshed)=>{
  q.preview=null;
  if(result.verified!==true||result.changed!==true){entry.state='needs_review';q.stopped=true;q.reason='The selected write has no verified completion. No remaining button was submitted.';return q.reason;}
  entry.state='completed';
  if(!refreshed){q.stopped=true;q.reason='The selected button was verified, but metadata could not be refreshed. Stop here and read the full table before planning remaining changes.';return q.reason;}
  const current=controllerViews.get(id),expected=q.rows.map(r=>r.slot===entry.slot?preview.desired:r.raw);
  if(!current?.assignment_read_back||current.slots.length!==4||current.slots.some((row,index)=>row.raw!==expected[index])||current.revision===q.revision){q.stopped=true;q.reason='The refreshed full table does not match the exact reviewed change. No remaining button was submitted.';return q.reason;}
  q.revision=current.revision;q.rows=current.slots.map(r=>({...r}));
  return 'Button '+entry.slot+' verified by full-table readback. Review the next remaining button explicitly.';
 }});}catch(error){
  if(q.running){entry.state='needs_review';q.stopped=true;q.reason='The selected write outcome is uncertain. Review Operations and read the full table. No remaining button was submitted.';}
  else{q.stopped=true;q.reason='The review can no longer continue: '+error.message;}
  throw error;
 }finally{if(entry.state==='submitting'){entry.state='needs_review';q.stopped=true;q.reason='Connection changed before the complete result was received. Review Operations and read the full table; no remaining button was submitted.';}q.running=false;if(controllerOpenQueue===id)renderControllerQueue(id);}
}
function renderControllerQueue(id){
 const q=controllerQueues.get(id);if(!q||controllerOpenQueue!==id)return;
 if(!q.running&&!q.stopped&&!controllerQueueCurrent(id,q)){q.stopped=true;q.reason='Assignments or connection changed outside this review. Read the full table and start a new review; no remaining button was submitted.';}
 const panel=$('proposal'),completed=q.entries.filter(e=>e.state==='completed').length,remaining=q.entries.filter(e=>e.state==='pending').length;
 panel.replaceChildren(node('h3','Button sequence for '+id),node('p',completed+' verified change(s) · '+remaining+' remaining · '+q.entries.filter(e=>e.state==='unchanged').length+' already matched.'));
 for(const entry of q.entries)panel.append(node('p',controllerSlotLabel(entry)+' · '+entry.state.replaceAll('_',' ')));
 if(q.reason)panel.append(node('p',q.reason));
 if(q.running)panel.append(node('p','Request in progress. Do not repeat or close it to retry.'));
 else if(!q.stopped&&remaining){
  if(q.preview)panel.append(node('p','Reviewed against full-table revision '+q.revision.slice(0,12)+'. '+controllerSlotLabel(q.preview.entry)),button('Apply this reviewed button',()=>applyControllerQueue(id),!controllerAllowed(id,'edit_assignment')));
  else panel.append(button('Preview next button',()=>previewControllerQueue(id),!controllerAllowed(id,'edit_assignment')));
 }
 if(!q.running)panel.append(button(q.stopped?'Discard unsent review plan':'Close sequence',()=>{controllerQueues.delete(id);controllerQueueDrafts.delete(id);controllerOpenQueue=null;panel.replaceChildren();return 'Review plan closed. Completed device changes remain in place; no recovery or write was retried.';}));
}
function renderControllerWorkflows(views){
 if(!Array.isArray(views))return;
 controllerViews.clear();for(const v of views)controllerViews.set(v.device_id,v);
 const root=$('buttons');root.replaceChildren();
 if(!views.length){root.append(node('p','No controllers added.'));return;}
 for(const v of views){
  const id=v.device_id,job=(status.jobs||{})[id];
  const oldEvidence=controllerNativeEvidence.get(id);
  if(oldEvidence?.view!==v){const observed=v.observed_target;controllerNativeEvidence.set(id,{view:v,seen:Date.now(),deadline:Date.now()+Math.max(0,Number(observed?.valid_for_ms)||0),instance:status.instance,epoch:connectionEpoch,token:currentToken()});}
  if(controllerAwaiting.get(id)&&job?.id===controllerAwaiting.get(id)&&job.state==='not_started'&&controllerNotStarted(job)){
   controllerOutcomes.set(id,job.message||'The operation was not started. Refresh and review before trying again.');
   controllerAwaiting.delete(id);controllerPendingActions.delete(id);
  }else if(controllerAwaiting.get(id)&&job?.id===controllerAwaiting.get(id)&&['completed','needs_review','interrupted','failed_needs_review'].includes(job.state)){
   const observedAccepted=!!job.result?.transaction&&v.accepted_native_transaction===job.result.transaction;
   const needsAcceptance=job.result?.acceptance_required===true&&!observedAccepted,needsReview=job.result?.requires_review===true||job.result?.verified===false;
   controllerOutcomes.set(id,observedAccepted?'Observed target behavior explicitly accepted. Stored assignment readback remains unsupported.':needsAcceptance?'Desired target behavior observed, not stored assignment readback. Review Operations to explicitly accept this limited evidence.':job.state==='completed'&&!needsReview?'Operation completed. Review the refreshed evidence below.':'Operation needs review. No request is repeated automatically.');
   if(job.state==='completed'&&!needsAcceptance&&!needsReview||controllerPendingActions.get(id)==='read_assignments'){controllerAwaiting.delete(id);controllerPendingActions.delete(id);}
  }
  const card=node('div');card.className='card';
  const device=inv.devices.find(d=>d.device_id===id);
  card.append(node('h3',(device?.name||'Controller '+id)+' · '+v.model),node('small','Controller '+id+' · Home Assistant: '+String(v.ha_status).replaceAll('_',' ')));
  card.append(node('p',controllerEventText(v)));
  card.append(node('p',v.assignment_read_back?'Assignments verified in this connection.':'Stored assignments are unknown; observed commands do not prove the stored table.'));
  if(controllerOutcomes.has(id)){const outcome=node('p',controllerOutcomes.get(id));outcome.setAttribute('role','status');card.append(outcome);}
  if(job?.state==='running')card.append(node('p','Operation running. '+((status.keypad_progress||{})[id]||'Waiting for evidence.')));
  if([126,127].includes(v.product_id)){
   controllerAction(card,v,'read_assignments','Read button assignments','keypad/read',{target:id});
   for(const row of v.slots){const line=node('div');line.append(node('p','Button '+row.slot+': '+String(row.status).replaceAll('_',' ')+(row.status==='unused'?'':' · target '+row.target+' · preset '+row.preset+'/255')+(row.preserved_by_setup?' · preserved by HA setup':'')));
    line.append(button('Edit button '+row.slot,()=>openControllerEditor(id,row.slot),!row.editable||!controllerAllowed(id,'edit_assignment')||inv.groups.some(g=>g.protected&&g.group_id===row.target)));card.append(line);}
   if(!v.actions.edit_assignment.enabled)card.append(node('small',controllerReason(v.actions.edit_assignment.reason)));
   card.append(button(controllerQueues.has(id)?'Open button review sequence':'Plan several button changes',()=>openControllerQueue(id),!controllerQueues.has(id)&&!controllerAllowed(id,'edit_assignment')));
  }
  if([91,126,127].includes(v.product_id))controllerAction(card,v,'setup_ha',v.product_id===91?'Set up fresh Accessory HA target':'Set up unused HA buttons','controller/setup',{target:id},v.product_id===91?'Assign this freshly claimed Accessory to an owned private HA event target? This changes its native destination; it does not clear an assignment.':'Set up only unused buttons on '+(device?.name||id)+' with owned HA event targets? Occupied and unknown slots will be preserved.');
  if(v.product_id===91)controllerAction(card,v,'local_input',v.input_mode==='local_level_reports'?'Use native command input':'Use local level input','controller/local-mode',{target:id,enabled:v.input_mode!=='local_level_reports'},'Change only the Accessory event decoder? This does not read, clear or change native assignments and does not prove HA-only operation.');
  if([81,91].includes(v.product_id)){card.append(node('p','Input: '+String(v.input_mode).replaceAll('_',' ')+' · '+String(v.input_status).replaceAll('_',' ')));if(v.observed_target)card.append(node('p','Observed command target '+v.observed_target.target+'. This is behavior evidence, not assignment readback.'));card.append(button('Review native target change',()=>openControllerNativeEditor(id),!controllerAllowed(id,'assign_native_target')||!controllerNativeFresh(id,v.observed_target?.baseline)));if(!v.actions.assign_native_target.enabled)card.append(node('small',controllerReason(v.actions.assign_native_target.reason)));card.append(node('small',controllerReason(v.actions.clear_native_assignment.reason)));}
  for(const note of v.notes||[])card.append(node('small',note));
  for(const recovery of v.recovery||[]){card.append(node('p','Recovery: '+recovery.kind+' · '+recovery.status+'. '+recovery.warning));if(recovery.mode==='explicit_guarded_continuation')card.append(node('strong','Continuing keypad setup may write previously unattempted slots. It is not a read-only check.'));card.append(button('Review operation and evidence',async()=>{$('recoveryPanel').open=true;return operations();}));}
  root.append(card);
 }
 if(controllerOpenQueue!==null&&controllerQueues.has(controllerOpenQueue))renderControllerQueue(controllerOpenQueue);
}
'''
