'use strict';
/* Operations review, recovery and device tools for the redesigned page.
   Everything the previous interface offered for unfinished work lives here.
   This page never retries a write: every button below either reads, observes
   or runs one explicit step that the bridge itself offered for that receipt. */

/* ---------- operations: data ---------- */
const Ops = {list:null, loading:false, error:'', busy:null, results:new Map(), seenPending:null, seenPublicationPending:null, showHistory:false, selected:null, loadedAt:0};
const OPS_PENDING = ['prepared', 'attempted', 'uncertain'];
const OPS_KINDS = {claim:'Pairing', claim_pre_key:'Unpaired check after a stopped pairing', remove:'Removal', external_reset:'Reset check', keypad_edit:'Keypad button change',
  keypad_setup:'Keypad Home Assistant setup', accessory_setup:'Accessory dimmer setup', accessory_native_setup:'Direct dimmer setup', anyplace_setup:'Anyplace dimmer setup',
  dimmer_edit:'Dimmer target change', group_membership:'Group membership change', scene:'Scene change'};
const OPS_STATUS = {prepared:'Prepared, nothing sent', attempted:'Started, not finished', uncertain:'Outcome not confirmed',
  verified:'Finished and verified', cancelled:'Closed without a change', retired:'Retired; record kept'};
const OPS_REASONS = {
  partial_keypad_setup_read_or_continue:'Read and close keeps assignments already stored, using two complete reads and no writes. Continue setup is a separate action that may program untried buttons.',
  imported_removal_owner_confirmation:'This light came from a cloud import, so its original hardware identity was not retained. Check for newly unpaired lights, then confirm which one is the light you saw reset. The removal command is never sent again.',
  removal_publication_pending:'Removal is confirmed and the address is retired. Refresh the bridge’s publication state; this sends no removal request.',
  no_dispatch_attempt_recorded:'Nothing was sent for this operation. It can be closed safely.',
  terminal_receipt_retained:'Finished. The record is kept for history.',
  read_only_membership_verification:'The bridge can read the light\'s group list back to see what was stored.',
  qualified_membership_readback_required:'This light type cannot be read back yet, so the bridge cannot confirm the change.',
  'evidence_required; ambiguous writes are not retried':'The bridge does not know whether the change was stored. It can check without sending the change again.',
  'fresh_proof_required; no association retry':'Pairing stopped part-way. After you reset the device by hand, the bridge can confirm it is unpaired again.',
  'first_device_only; explicit owner reset and fresh unpaired proof required':'Pairing of the first light stopped part-way. After you reset it by hand, the bridge can confirm it is unpaired again.',
  authenticate_reserved_address_after_uncertain_association:'Pairing may have completed. The bridge can check whether the device answers at its new address.',
  'observation_only; owner reset requires fresh exact unpaired identity':'The bridge can only watch what the dimmer does now. It cannot read the stored setting.',
  review_anyplace_attempt_state:'This Anyplace setup record needs a closer look before anything else is tried.',
  no_qualified_recovery_path:'There is no safe automatic check for this one yet.'};
const OPS_STEP_REASONS = {
  retire_pre_key_claim:'Pairing stopped before the bridge sent this light an address or network key, so it cannot have joined. The bridge can confirm it still shows as unpaired.',
  retire_pre_network_key_claim:'Pairing stopped after an address was proposed but before the network key, so the light cannot have joined. The bridge can confirm it shows as unpaired again.'};
const OPS_ACTIONS = {
  resolve_keypad_setup:{label:'Read and close partial setup', path:'/api/keypad/resolve-setup', partialResolution:true,
    confirm:t => `Read and close partial setup on ${opsDeviceName(t)}? The bridge takes two complete button readings and keeps assignments already stored. It sends no button changes, does not reset the keypad, and does not resume setup automatically. Home Assistant sharing stays unchanged and reserved targets are kept.${t.detail?.product_id === 126 ? ' Wait for each wake prompt, then press the bottom Power/Off button once.' : ''}`},
  refresh_removed_publication:{label:'Refresh publication cleanup', path:'/api/reconcile',
    confirm:() => 'Refresh publication after this confirmed removal? This does not scan, reset, pair or send removal again.'},
  cancel_unattempted:{label:'Close it (nothing was sent)', path:'/api/cancel',
    confirm:() => 'Close this operation? Nothing was sent to the device, so nothing changes on it.'},
  reconcile:{label:'Check what happened', path:'/api/reconcile',
    confirm:t => t.kind === 'claim' && [81,126].includes(t.detail?.product_id) ? opsClaimConfirmation(t) : t.kind === 'keypad_setup'
      ? `Continue Home Assistant setup on ${opsDeviceName(t)}? The bridge reads the buttons first, then may write buttons it has not tried yet. A button with an unknown outcome is never written again.`
      : `Check ${opsDeviceName(t)} now? This only reads or observes. It does not send the change again.`},
  reconcile_claim:{label:'Check whether pairing finished', path:'/api/reconcile',
    confirm:t => [81,126].includes(t.detail?.product_id) ? opsClaimConfirmation(t) : `Check whether ${opsDeviceName(t)} answers at its new address? This does not pair or reset anything.`},
  resume_unattempted_insertion:{label:'Finish the button change', path:'/api/keypad/resume',
    confirm:t => `Finish the change on ${opsDeviceName(t)}? The old assignment was removed and verified, and the new one was never sent. This writes the new assignment once and reads it back.`},
  retire_pre_key_claim:{label:'Confirm the light is still unpaired', path:'/api/claim/pre-key-retire', retire:true,
    confirm:() => 'This pairing stopped before the bridge sent an address or network key. Check that this exact light still shows as unpaired and set the failed address aside for good? This does not reset, pair or retry the light.'},
  retire_pre_network_key_claim:{label:'Confirm the light is unpaired again', path:'/api/claim/pre-key-retire', retire:true,
    confirm:t => `This pairing stopped after the bridge proposed address ${t.target} but before the network key, so the light cannot have joined. Check that this exact light shows as unpaired again and retire that address for good? This does not reset, pair or retry the light.`},
  verify_bootstrap_owner_reset:{label:'I reset the first light: verify it', path:'/api/claim/reset-reconcile', ownerReset:true,
    confirm:() => 'Have you physically reset the first light yourself? This checks that it shows as unpaired and retires the failed address. It does not reset or pair anything.'},
  verify_claim_owner_reset:{label:'I reset the controller: verify it', path:'/api/claim/reset-reconcile', ownerReset:true,
    confirm:() => 'Have you physically reset this exact wired controller yourself? This checks that it shows as unpaired and retires the failed address. It does not retry pairing.'},
  observe_owner_reset:{label:'I reset the device: verify it', path:'/api/reset-reconcile', ownerReset:true,
    confirm:() => 'Have you physically reset this exact device yourself? This only looks for it as unpaired. It sends no reset.'},
  accept_fresh_observation:{label:'Accept what the dimmer does now', path:'/api/dimmer/accept',
    confirm:t => `Accept the behaviour just observed from ${opsDeviceName(t)}? This records what was seen. It is not a read of the stored setting.`}};

function opsDeviceName(t) { const d = typeof dev === 'function' ? dev(t.target) : null; return d ? `${d.name} (#${t.target})` : `device #${t.target}`; }
function opsClaimConfirmation(t) {
  const wake = t.detail.product_id === 126 ? 'bottom Power/Off button' : 'small Wake-Up button on the bottom';
  return `Check whether pairing finished for ${opsDeviceName(t)}? Wait for the wake prompt on this page before pressing its ${wake} once. Verification can wait up to two minutes for a wake. This checks the existing pairing; it does not pair or reset anything.`;
}
function opsClaimProgress(t, r) {
  if (t.kind !== 'claim' || Ops.busy !== t.id || !r?.pending ||
      !['reconcile','reconcile_claim'].includes(r.action) || r.target !== t.target ||
      ![81,126].includes(t.detail?.product_id) || r.product !== t.detail.product_id ||
      !Number.isInteger(t.detail?.uuid_hash) || r.uuidHash !== t.detail.uuid_hash) return r?.text;
  if (r.instance !== S.status?.instance) return 'The bridge restarted during verification. Review Operations before another check; nothing is repeated automatically.';
  const progress = S.status?.claim_progress?.[t.target];
  if (progress?.uuid_hash !== r.uuidHash) return r.text;
  const battery = r.product === 126;
  return {
    waiting_for_wake:battery ? 'Press the bottom Power/Off button once now to wake the battery keypad. Keep this page open and wait for verification.'
      : 'Press the small Wake-Up button on the bottom once now. Keep this page open and wait for verification.',
    settling:'Wake received. Let the controller settle; do not press another button yet.',
    verifying:'Wake received. Checking the encrypted pairing reply; wait for the result.',
    verified:'Pairing reply verified. Finishing the device list update…',
    needs_review:'Pairing verification is not confirmed. Keep this receipt and review Operations before another check. Nothing is repeated automatically.'
  }[progress.phase] || r.text;
}
function opsKeypadRecoveryProgress(t) {
  const job = S.status?.jobs?.[t.target], p = job?.progress;
  if (t.kind !== 'keypad_setup' || !OPS_PENDING.includes(t.status) || job?.state !== 'running' ||
      !p || p.transaction !== t.id || p.target !== t.target || p.job_id !== job.id ||
      p.product_id !== 126 || t.detail?.product_id !== 126 ||
      typeof p.instance !== 'string' || p.instance !== S.status.instance ||
      !['keypad_setup','keypad_recovery'].includes(p.operation)) return '';
  return typeof batteryPhaseText === 'function' ? batteryPhaseText(t.target) : '';
}
function opsPartialClosed(t) {
  return t.kind === 'keypad_setup' && t.status === 'cancelled' && t.detail?.partial_resolution;
}
function removalPublicationPending(t) { return ['remove','external_reset'].includes(t.kind) && t.status === 'verified' && t.detail?.publication_pending === true; }
function opsPending() { return (Ops.list || []).filter(t => OPS_PENDING.includes(t.status) || removalPublicationPending(t)); }
function opsAllowed() {
  return !Ops.busy && !Ops.loading && managementFresh() && !S.radio.label && !S.radio.queue.length && S.status?.preview_only !== true;
}
async function loadOperations() {
  if (Ops.loading) return;
  Ops.loading = true; Ops.error = ''; renderSoon();
  try {
    const data = await api('/api/transactions');
    Ops.list = Array.isArray(data.transactions) ? data.transactions : [];
    Ops.loadedAt = Date.now(); Ops.seenPending = S.status?.pending_transactions ?? null; Ops.seenPublicationPending = S.status?.publication_pending ?? 0;
  } catch (e) { Ops.error = 'Could not load operations. ' + e.message; }
  Ops.loading = false; renderSoon();
}
function opsOutcome(t, action, result) {
  if (action.partialResolution && !(result.accepted === true && result.job_id)) {
    const slots = ['applied_slots','unchanged_attempted_slots','unattempted_slots'];
    const valid = result.partial_resolution === true && result.resolved === true &&
      result.assignment_read_back === true && result.write_retried === false &&
      result.setup_automatically_resumed === false && result.exposure_changed === false &&
      result.reservations_retained === true && slots.every(key => Array.isArray(result[key]) &&
        result[key].every(slot => Number.isInteger(slot) && slot >= 1 && slot <= 4)) &&
      new Set(slots.flatMap(key => result[key])).size === slots.flatMap(key => result[key]).length;
    if (!valid) return {review:true, text:'Read-only closure is not confirmed. Keep this receipt and review Operations; no setup is resumed automatically.'};
    const buttons = values => values.length ? values.join(', ') : 'none';
    return {review:result.requires_review === true || result.publication_pending === true,
      text:`Closed; verified assignments kept. Applied buttons: ${buttons(result.applied_slots)}. Attempted but unchanged: ${buttons(result.unchanged_attempted_slots)}. Not attempted: ${buttons(result.unattempted_slots)}. No button changes were sent. Setup was not resumed; sharing is unchanged and reserved targets are kept.${result.requires_review === true || result.publication_pending === true ? ' Bridge publication still needs review.' : ''}`};
  }
  if (result.owner_confirmation_required === true) return {
    review:true, ownerConfirmation:true,
    removal_candidates:validRemovalCandidates(result.removal_candidates),
    text:'The removal request was sent once. The bridge cannot automatically link an unpaired device to the imported entry. Confirm the light you saw reset below, or check again without sending removal again.'};
  if (result.removed === true && (result.verification_mode === 'owner_confirmed_unpaired' || t.detail?.identity_source === 'cloud_import')) return {
    review:result.publication_pending === true || result.requires_review === true,
    text:'Removal completed using your confirmation and a fresh unpaired-device check.' + (result.publication_pending ? ' Publication cleanup still needs review.' : ' The device is no longer listed on this bridge.')};
  if (result.removed === true) return result.publication_pending || result.requires_review
    ? {review:true, text:'Removal is confirmed and the address is retired. Bridge publication cleanup still needs review; no removal request is repeated.'}
    : {text:'Removal is confirmed and bridge publication cleanup is complete. No removal request was repeated.'};
  if (action.retire) {
    if (result.retired !== true || result.pre_key_retired !== true || result.address_retired !== true ||
        result.association_retried !== false || result.automatic_reclaim !== false)
      return {review:true, text:'Safe recovery is not confirmed. Keep this record and do not reset or pair this light again yet.'};
    if (result.requires_review || result.publication_pending)
      return {review:true, text:'The failed attempt and its address are safely set aside, but the bridge still needs a review before another pairing. No reset or pairing was sent.'};
    return {text:'Cleared. The record and the failed address are kept. No reset or pairing was sent; add the light again when ready.'};
  }
  if (t.kind === 'claim' && result.verified === true) {
    const added = result.associated === true && result.device_id === t.target;
    const pending = result.publication_pending === true || result.requires_review === true;
    return {review:pending, text:'Pairing verified. ' + (added ? 'The device was added to the bridge. ' : 'Refresh the device list to check its entry. ') +
      (pending ? 'Publication still needs review. ' : '') + 'Pairing was not repeated.'};
  }
  if (t.kind === 'claim' && (result.verified === false || result.requires_review === true))
    return {review:true, text:'Pairing is still not confirmed. Keep this receipt and review Operations before another check. No pairing or reset was sent.'};
  if (result.accepted === true && result.job_id) return {job:result.job_id, text:'Check started. The result appears here when it finishes.'};
  if (result.cancelled === true) return {text:'Closed. Nothing was sent to the device.'};
  if (result.verified === true) return {text:'Verified. The device holds the intended change. Nothing was sent again.'};
  if (result.unchanged === true) return {text:'Nothing was stored and the original setting is intact. This operation is closed; you can make the change again.'};
  if (result.resumable === true) return {text:'The removal is confirmed and the new assignment was never sent. Use "Finish the button change" to complete it.'};
  if (result.accepted === true) return {text:'Accepted. The record now shows the observed behaviour.'};
  if (result.requires_review === true || result.verified === false)
    return {review:true, text:'Still not confirmed. Nothing was sent again. Try the check once the device is powered and in range, or reset the device by hand if the bridge offers that.'};
  return {text:'The bridge finished this step. Refresh to see where the operation stands.'};
}
async function runOperation(id, name) {
  const t = (Ops.list || []).find(x => x.id === id), action = OPS_ACTIONS[name];
  if (!t || !action || !(t.recovery?.actions || []).includes(name) || !opsAllowed() || S.status?.jobs?.[t.target]?.state === 'running') return;
  if (!confirm(action.confirm(t))) return;
  const instance = S.status.instance;
  Ops.busy = id; Ops.results.set(id, {text:t.kind === 'claim' && [81,126].includes(t.detail?.product_id) ? 'Starting pairing verification. Wait for the wake prompt…' : 'Working…',
    pending:true, instance, target:t.target, action:name, product:t.detail?.product_id, uuidHash:t.detail?.uuid_hash}); renderSoon();
  try {
    const body = action.ownerReset ? {transaction:id, owner_reset:true} : {transaction:id};
    const outcome = opsOutcome(t, action, await api(action.path, body));
    Ops.results.set(id, instance !== S.status.instance
      ? {job:null, pending:false, review:true, target:t.target, instance,
          text:'The bridge restarted during the check. Refresh and review this operation again.'}
      : {...outcome, pending:!!outcome.job, target:t.target, instance, action:name});
  } catch (e) {
    const refused = e.details?.operation_started === false || e.status === 501;
    Ops.results.set(id, {review:!refused, text:(refused ? 'Not started. ' : 'The outcome of this step is not confirmed. Nothing is repeated automatically. ') + e.message});
  }
  Ops.busy = null;
  await refreshSnapshot(); await loadOperations();
}
function validRemovalCandidates(rows) {
  if (!Array.isArray(rows)) return [];
  const seen = new Set();
  return rows.filter(row => row && typeof row.uuid === 'string' && /^[a-f0-9]{32}$/i.test(row.uuid) &&
    Number.isInteger(row.product_id) && !seen.has(row.uuid.toLowerCase()) && seen.add(row.uuid.toLowerCase()))
    .map(row => ({uuid:row.uuid.toLowerCase(), product_id:row.product_id}));
}
function importedRemovalConfirmationHtml(t, r, allowed) {
  if (t.kind !== 'remove' || t.detail?.identity_source !== 'cloud_import' || !OPS_PENDING.includes(t.status)) return '';
  const current = r?.ownerConfirmation === true && r.instance === S.status.instance;
  const candidates = current ? validRemovalCandidates(r.removal_candidates) : [];
  const canConfirm = candidates.length === 1 && allowed && S.status?.mesh_ready && (t.recovery?.actions || []).includes('confirm_imported_removal');
  return `<div class="callout warn"><div><strong>Confirm the imported light</strong>
    <p>The cloud import did not include its original hardware identity. A new unpaired light alone does not prove which light was reset. Confirm only after you identify it as the light you saw reset; otherwise leave this operation open.</p>
    ${candidates.length > 1 ? '<p>More than one newly unpaired light was found. Switch off unrelated unpaired lights, then choose “Check what happened” again. Confirmation is unavailable while the result is ambiguous.</p>' : ''}
    ${candidates.length ? candidates.map(row => {
      const address = row.uuid.slice(0,12).match(/../g).reverse().join(':').toUpperCase();
      return `<p>Unpaired light · ${esc(address)}</p><button class="btn" onclick="confirmImportedRemoval('${esc(t.id)}','${row.uuid}')" ${canConfirm ? '' : 'disabled'}>This is the light I saw reset…</button>`;
    }).join('') : '<p>Choose “Check what happened” to look for the light again. Nothing is reset or removed again.</p>'}
    </div></div>`;
}
async function confirmImportedRemoval(id, identity) {
  const t = (Ops.list || []).find(row => row.id === id), previous = Ops.results.get(id);
  if (!t || t.kind !== 'remove' || t.detail?.identity_source !== 'cloud_import' || !OPS_PENDING.includes(t.status) ||
      !(t.recovery?.actions || []).includes('confirm_imported_removal') || !opsAllowed() || !S.status?.mesh_ready ||
      previous?.ownerConfirmation !== true || previous.instance !== S.status.instance ||
      validRemovalCandidates(previous.removal_candidates).length !== 1 ||
      !validRemovalCandidates(previous.removal_candidates).some(row => row.uuid === identity)) return;
  const instance = S.status.instance;
  if (!confirm(`Have you identified this unpaired light as ${opsDeviceName(t)} and seen that light reset?\n\nThe bridge cannot prove the old and new identities match. Your confirmation plus a fresh unpaired-device check will remove the old entry. This sends no reset or pairing command. If unsure, cancel.`)) return;
  if (instance !== S.status.instance || !opsAllowed()) return;
  Ops.busy = id; Ops.results.set(id, {...previous, pending:true, text:'Checking the selected unpaired light…'}); renderSoon();
  try {
    const result = await api('/api/unclaim/confirm', {transaction:id, uuid:identity, owner_confirmed:true});
    Ops.results.set(id, instance !== S.status.instance
      ? {review:true, instance, text:'The bridge restarted during confirmation. Refresh and check this operation again.'}
      : {...opsOutcome(t, {}, result), instance, target:t.target});
  } catch (e) {
    Ops.results.set(id, {review:true, instance, text:'Removal confirmation did not finish. Nothing was reset again. ' + e.message});
  }
  Ops.busy = null; await refreshSnapshot(); await loadOperations();
}

function settleOperationJobs() {
  let reload = false;
  for (const [id, r] of Ops.results) {
    if (!r.job) continue;
    const job = S.status?.jobs?.[r.target];
    if (r.instance !== S.status.instance) {
      Object.assign(r, {job:null, pending:false, review:true, text:'The bridge restarted during the check. Refresh and review this operation again.'}); reload = true;
    } else if (job?.id === r.job && job.state !== 'running') {
      const ok = job.state === 'completed', notStarted = job.state === 'not_started' && job.operation_started === false;
      Object.assign(r, {job:null, pending:false, review:!ok && !notStarted,
        text:ok ? 'Check finished. See the operation\'s new status below.'
          : notStarted ? 'Check not started. ' + (job.message || '')
          : 'The check could not confirm the outcome. Nothing was sent again.'});
      if (ok && r.action === 'resolve_keypad_setup') {
        const t = (Ops.list || []).find(t => t.id === id);
        Object.assign(r, opsOutcome(t || {}, OPS_ACTIONS.resolve_keypad_setup, job.result || {}));
      }
      reload = true;
    }
  }
  if (reload && !Ops.loading) setTimeout(loadOperations, 0);
}

/* ---------- operations: view ---------- */
function opsSceneCard(t) {
  // Scene work runs as its own job, so these buttons use the same steps as the
  // Scenes page: a read-only check, an explicit continue, or setting aside.
  const known = typeof sceneOp === 'function', live = known ? sceneOp(t.id) : null;
  const d = (live ? sceneOpDetail(live) : t.detail) || {}, pending = OPS_PENDING.includes(t.status);
  const name = d.desired?.name || d.prior?.name || 'Scene', what = {create:'Scene save', edit:'Scene change', remove:'Scene removal'}[d.action] || 'Scene change';
  const badge = pending ? 'Needs review' : d.outcome === 'set_aside' ? 'Set aside; record kept' : d.completed === true ? 'Finished and verified'
    : d.outcome === 'unchanged' ? 'Closed; the lights were unchanged' : OPS_STATUS[t.status] || t.status;
  const working = known && sceneRunning(), allowed = opsAllowed() && !working, radio = allowed && S.status?.mesh_ready;
  const id = esc(t.id), silent = Number.isInteger(d.unreachable) ? esc(sceneLightName(d.unreachable)) : '';
  const reason = pending ? 'This scene work did not finish. Read and recover checks what the lights hold and sends no scene change. Continue save appears once that check passes. If a light cannot answer, you can set the work aside.'
    : d.outcome === 'set_aside' ? `You set this aside. Nothing more was sent.${d.set_aside?.reference_reserved ? ' Its scene slot stays reserved because a light may have received part of the change.' : ''}` : '';
  const buttons = !pending || !known ? '' : `<div class="row">
    ${d.recovery_ready === true ? `<button class="btn primary" onclick="resumeScene('${id}')" ${radio ? '' : 'disabled'}>${d.retry_requires_explicit_action ? 'Retry full preset and continue' : 'Continue save'}</button>` : ''}
    <button class="btn${d.recovery_ready === true ? '' : ' primary'}" onclick="recoverScene('${id}')" ${radio ? '' : 'disabled'}>Read and recover</button>
    <button class="btn" onclick="confirmSetAsideScene('${id}')" ${allowed ? '' : 'disabled'}>Set aside…</button>
    <button class="btn" onclick="go('scenes')">Open Scenes</button></div>`;
  return `<div class="card ops-card"><h3>${esc(what)} · ${esc(name)}</h3>
    <p><span class="badge ${pending ? 'warn' : d.completed === true ? 'ok' : ''}">${esc(badge)}</span></p>
    ${reason ? `<p>${esc(reason)}</p>` : ''}
    ${pending && silent ? `<p class="hint">${silent} did not answer the last check.</p>` : ''}
    ${buttons}
    ${working && pending ? '<p role="status" aria-live="polite"><span class="pulse"></span> Working… Open Scenes to follow progress.</p>' : ''}
    <details><summary>Technical record</summary><pre class="ops-pre">${esc(JSON.stringify(t, null, 2))}</pre></details></div>`;
}
function opsCard(t) {
  if (t.kind === 'scene') return opsSceneCard(t);
  const actions = t.recovery?.actions || [], r = Ops.results.get(t.id), allowed = opsAllowed() && S.status?.jobs?.[t.target]?.state !== 'running';
  const phase = opsKeypadRecoveryProgress(t);
  const reason = OPS_REASONS[t.recovery?.reason] || OPS_STEP_REASONS[actions.find(x => OPS_STEP_REASONS[x])] || t.recovery?.reason || '';
  const buttons = actions.filter(a => OPS_ACTIONS[a]).map(a =>
    `<button class="btn${a === actions[0] ? ' primary' : ''}" onclick="runOperation('${esc(t.id)}','${a}')" ${allowed ? '' : 'disabled'}>${esc(a === 'reconcile' && t.kind === 'keypad_setup' ? 'Continue setup (may write)' : OPS_ACTIONS[a].label)}</button>`).join('');
  const unknown = actions.filter(a => !OPS_ACTIONS[a] && a !== 'confirm_imported_removal');
  return `<div class="card ops-card"><h3>${esc(OPS_KINDS[t.kind] || t.kind)} · ${esc(opsDeviceName(t))}</h3>
    <p><span class="badge ${OPS_PENDING.includes(t.status) || removalPublicationPending(t) ? 'warn' : t.status === 'verified' ? 'ok' : ''}">${esc(removalPublicationPending(t) ? 'Removed; publication cleanup pending' : opsPartialClosed(t) ? 'Closed; verified assignments kept' : OPS_STATUS[t.status] || t.status)}</span></p>
    ${reason ? `<p>${esc(reason)}</p>` : ''}
    ${buttons ? `<div class="row">${buttons}</div>` : OPS_PENDING.includes(t.status) ? '<p class="hint">The bridge offers no safe step for this operation. It stays on record and blocks nothing by itself being listed here.</p>' : ''}
    ${importedRemovalConfirmationHtml(t, r, allowed)}
    ${unknown.length ? `<p class="hint">This bridge also lists steps this page does not know: ${esc(unknown.join(', '))}.</p>` : ''}
    ${r || phase ? `<p role="status" aria-live="polite">${r?.pending || phase ? '<span class="pulse"></span> ' : ''}${esc(phase || opsClaimProgress(t, r))}</p>` : ''}
    <details><summary>Technical record</summary><pre class="ops-pre">${esc(JSON.stringify(t, null, 2))}</pre></details></div>`;
}
function viewOperations() {
  if (Ops.list === null && !Ops.loading && !Ops.error) setTimeout(loadOperations, 0);
  settleOperationJobs();
  const count = S.status?.pending_transactions, cleanupCount = S.status?.publication_pending ?? 0;
  if (Ops.list !== null && !Ops.loading && !Ops.busy && ((Number.isInteger(count) && count !== Ops.seenPending) || cleanupCount !== Ops.seenPublicationPending)) setTimeout(loadOperations, 0);
  if (Ops.selected) {
    const transaction = Ops.list?.find(t => t.id === Ops.selected);
    return `<div class="view-head"><div><h2>Review operation</h2></div><button class="btn" onclick="go('operations')">All operations</button></div>
      ${Ops.error ? `<p role="alert">${esc(Ops.error)}</p><button class="btn" onclick="loadOperations()">Refresh</button>` : ''}
      ${transaction ? opsCard(transaction) : Ops.error ? '' : `<p role="status">${Ops.loading || Ops.list === null ? 'Loading this operation…' : 'This receipt is unavailable on this bridge.'}</p>`}`;
  }
  const pending = opsPending(), history = (Ops.list || []).filter(t => !OPS_PENDING.includes(t.status) && !removalPublicationPending(t)).reverse();
  return `<div class="view-head"><div><h2>Operations</h2><div class="sub">Changes the bridge started, and anything that needs your review</div></div>
    <div class="actions"><button class="btn" onclick="go('settings')">Back to Settings</button><button class="btn" onclick="loadOperations()" ${Ops.loading ? 'disabled' : ''}>${Ops.loading ? 'Loading…' : 'Refresh'}</button></div></div>
  ${Ops.error ? `<div class="callout warn"><div>${esc(Ops.error)}</div></div>` : ''}
  ${Ops.list === null ? '<p class="empty">Loading operations…</p>' : `
  <div class="callout info">${I.info}<div>Nothing here is retried automatically. A check only reads or observes; any step that writes says so before you confirm it.</div></div>
  <h3 class="ops-h">Needs review (${pending.length})</h3>
  ${pending.length ? `<div class="cards">${pending.map(opsCard).join('')}</div>` : '<p class="empty">Nothing is waiting for review.</p>'}
  <h3 class="ops-h">History (${history.length})</h3>
  ${history.length ? `<p><button class="btn sm" onclick="Ops.showHistory=!Ops.showHistory;renderSoon()">${Ops.showHistory ? 'Hide history' : 'Show history'}</button></p>
    ${Ops.showHistory ? `<div class="cards">${history.slice(0, 60).map(opsCard).join('')}</div>${history.length > 60 ? `<p class="hint">Showing the latest 60 of ${history.length}.</p>` : ''}` : ''}` : '<p class="empty">No finished operations yet.</p>'}`}`;
}
function opsReviewButton(receipt) {
  if (!receipt || typeof receipt !== 'string') return `<button class="btn" onclick="go('operations')">Review Operations</button>`;
  return `<button class="btn" onclick="reviewOperation(${esc(JSON.stringify(receipt))})">Review this operation</button>`;
}
function reviewOperation(receipt) {
  Ops.list = null; Ops.error = '';
  go('operations', receipt);
}

/* ---------- settings cards ---------- */
const OpsBackup = {message:'', busy:false};
async function exportInventory() {
  if (OpsBackup.busy) return;
  OpsBackup.busy = true; OpsBackup.message = 'Preparing the file…'; renderSoon();
  try {
    const data = await api('/api/inventory');
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type:'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = 'halo-inventory-' + new Date().toISOString().slice(0, 10) + '.json';
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
    OpsBackup.message = `Saved a list of ${(data.devices || []).length} devices and ${(data.groups || []).length} groups. It holds names and IDs only, no keys.`;
  } catch (e) { OpsBackup.message = 'Could not export. ' + e.message; }
  OpsBackup.busy = false; renderSoon();
}
async function importInventory(input) {
  const file = input.files && input.files[0]; input.value = '';
  if (!file || OpsBackup.busy) return;
  let data;
  try { data = JSON.parse(await file.text()); } catch (e) { OpsBackup.message = 'That file is not valid JSON. Nothing was changed.'; renderSoon(); return; }
  if (!data || !Array.isArray(data.devices) || !Array.isArray(data.groups)) { OpsBackup.message = 'That file is not an inventory export. Nothing was changed.'; renderSoon(); return; }
  if (!confirm(`Replace the bridge's device and group list with this file (${data.devices.length} devices, ${data.groups.length} groups)?\n\nThis changes names, groups and what is listed on the bridge only. It does not pair, reset or reprogram any device. Saved keypad readings are cleared until they are read again.`)) return;
  OpsBackup.busy = true; OpsBackup.message = 'Importing…'; renderSoon();
  try { await api('/api/import', data); OpsBackup.message = 'Imported. The list below now matches the file.'; }
  catch (e) { OpsBackup.message = (e.status === 400 ? 'The bridge refused that file. Nothing was changed. ' : 'Import outcome not confirmed. Refresh and check the device list. ') + e.message; }
  OpsBackup.busy = false; await refreshSnapshot(); renderSoon();
}
function opsSettingsCards() {
  const n = S.status?.pending_transactions, cleanup = S.status?.publication_pending || 0;
  return `<div class="card"><h3>Operations</h3>
     <p>${n ? `${n} unfinished operation${n === 1 ? ' needs' : 's need'} review.` : 'No unfinished device changes.'}${cleanup ? ` ${cleanup} confirmed removal${cleanup === 1 ? '' : 's'} need${cleanup === 1 ? 's' : ''} publication cleanup.` : ''} Past pairing, keypad, dimmer and group changes are kept here too.</p>
     <div class="row"><button class="btn${n || cleanup ? ' primary' : ''}" onclick="go('operations')">${n || cleanup ? 'Review operations' : 'Open operations'}</button></div></div>`;
}
function opsInventoryCard() {
  const ok = managementFresh() && !S.radio.label && !OpsBackup.busy && S.status?.preview_only !== true;
  return `<div class="card"><h3>Device list backup</h3>
     <p>Save the bridge's list of devices, groups and names to a file, or load one back. The file holds no keys or passwords.</p>
     <div class="row"><button class="btn" onclick="exportInventory()" ${OpsBackup.busy ? 'disabled' : ''}>Export list</button>
       <label class="btn" ${ok ? '' : 'aria-disabled="true" style="opacity:.5;pointer-events:none"'}>Import list…<input type="file" accept="application/json,.json" style="display:none" onchange="importInventory(this)" ${ok ? '' : 'disabled'}></label></div>
     ${OpsBackup.message ? `<p role="status">${esc(OpsBackup.message)}</p>` : ''}</div>`;
}

/* ---------- device tools (drawer) ---------- */
const OpsTools = new Map();
function opsTool(id) { if (!OpsTools.has(id)) OpsTools.set(id, {}); return OpsTools.get(id); }
function opsToolsAllowed(id) { const t = opsTool(id); return !t.busy && managementFresh() && !S.radio.label && !S.radio.queue.length && S.status?.preview_only !== true; }
async function inspectDevice(id) {
  if (!opsToolsAllowed(id)) return;
  const t = opsTool(id); t.busy = true; t.message = 'Asking the device who it is…'; renderSoon();
  try { t.identity = await api('/api/examine', {target:id}); t.message = 'Identity details loaded.'; }
  catch (e) { t.message = 'Could not inspect. ' + e.message; }
  t.busy = false; renderSoon();
}
async function captureDevice(id) {
  if (!opsToolsAllowed(id)) return;
  const t = opsTool(id); t.busy = true; t.message = 'Starting a 30-second recording…'; renderSoon();
  try { t.capture = await api('/api/capture', {target:id, seconds:30}); t.message = 'Recording radio messages from this device for 30 seconds. Use the device now, then stop and save.'; }
  catch (e) { t.message = 'Recording not started. ' + e.message; }
  t.busy = false; renderSoon();
}
async function finishCapture(id, keep) {
  const t = opsTool(id); if (!t.capture || t.busy) return;
  t.busy = true; renderSoon();
  try {
    const data = await api('/api/capture/action', {id:t.capture.id, action:keep ? 'stop' : 'release'});
    if (keep) {
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type:'application/json'}));
      const link = document.createElement('a'); link.href = url; link.download = 'halo-capture-' + t.capture.id + '.json';
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
    t.message = keep ? 'Recording saved to a file.' : 'Recording discarded.'; t.capture = null;
  } catch (e) { t.message = 'Could not finish the recording. ' + e.message; }
  t.busy = false; renderSoon();
}
async function removeDevice(id) {
  const d = dev(id); if (!d || !opsToolsAllowed(id) || opsTool(id).removalUncertain || S.status?.capabilities?.unclaim !== true) return;
  const t = opsTool(id); t.busy = true; t.message = 'Checking removal requirements…'; renderSoon();
  let submitted = false;
  const instance = S.status.instance;
  try {
    const preview = await api('/api/unclaim/preview', {target:id});
    if (instance !== S.status.instance) throw new Error('The bridge restarted. Refresh and review removal again.');
    if (preview.target !== id || typeof preview.revision !== 'string' || !/^[a-f0-9]{64}$/.test(preview.revision))
      throw new Error('The bridge did not provide a valid removal review.');
    if (preview.eligible !== true || preview.enabled !== true) {
      const reasons = {active_relay_requires_reconnection:'This device is the bridge’s current Bluetooth relay. Connect through another device first.',
        native_controller_targets_device:'A controller targets this device directly. Change that assignment first.',
        native_controller_group_would_be_empty:'This would empty a group used by a native controller. Change its assignment first.'};
      t.message = 'Removal is not available. ' + ((preview.blockers || []).map(reason => reasons[reason] || reason).join(' ') || 'The bridge may be busy or an operation needs review.');
      t.review = true;
      return;
    }
    const groups = (preview.affected_groups || []).map(g => `${g.name} (${g.members.length - 1} light${g.members.length - 1 === 1 ? '' : 's'} remaining)`).join(', ');
    const impact = groups ? `\n\nRemoved from these groups: ${groups}.` : '';
    const native = (preview.native_dependencies || []).length ? '\nNative controller assignments to these groups will remain in place.' : '';
    const verification = preview.requires_owner_confirmation === true
      ? 'This light was cloud-imported without its original hardware identity. The bridge checks that it answers, scans for unpaired devices, then sends removal once. You must identify and confirm the light you saw reset in Operations before its old entry is removed. Keep it powered on; no removal is retried.'
      : 'The bridge sends one removal request, then checks automatically that it is unpaired. Keep it powered on. It stays listed until verified; the removal command is never repeated.';
    if (!confirm(`Remove ${d.name} from this mesh?${impact}${native}\n\nHome Assistant automations have not been checked. ${verification}`)) {
      t.message = 'Removal cancelled. No removal request was sent.';
      t.review = false;
      return;
    }
    if (instance !== S.status.instance || !managementFresh() || S.radio.label || S.radio.queue.length || S.status?.preview_only === true)
      throw new Error('The page changed or became busy. Refresh and review removal again.');
    t.message = 'Sending removal and verifying unpaired status…'; renderSoon(); submitted = true; t.removalUncertain = true;
    const result = await api('/api/unclaim', {target:id, revision:preview.revision});
    if (instance !== S.status.instance) throw new Error('The bridge restarted during removal. Review Operations before another change.');
    const confirmed = result.removed === true || result.verified === true;
    t.removalUncertain = !confirmed;
    t.receipt = result.transaction;
    t.message = confirmed ? (result.publication_pending ? 'Removal is confirmed. Bridge publication cleanup needs review in Operations.' : 'Removed and confirmed unpaired.')
      : result.owner_confirmation_required === true ? 'Removal was sent once. Open Operations to identify and confirm the light you saw reset. The bridge cannot automatically match the imported entry to an unpaired light.'
      : 'Removal was sent, but automatic verification did not confirm the device is unpaired. Keep it powered on and choose Check what happened in Operations. The removal command will not be repeated.'; t.review = !confirmed || result.publication_pending === true;
    if (result.owner_confirmation_required === true && typeof result.transaction === 'string') {
      Ops.results.set(result.transaction, {...opsOutcome({kind:'remove'}, {}, result), instance, target:id});
    }
  } catch (e) {
    const refused = !submitted || e.details?.operation_started === false || e.status === 501;
    t.receipt = e.details?.transaction;
    if (refused) t.removalUncertain = false;
    t.message = (refused ? 'Removal not started. ' : 'Removal outcome not confirmed. Nothing is repeated automatically. ') + e.message; t.review = !refused || e.details?.requires_review === true;
  } finally {
    t.busy = false; await refreshSnapshot(); renderSoon();
  }
}
function deviceToolsHtml(d) {
  const id = d.avion_id, t = opsTool(id), ok = opsToolsAllowed(id), removable = S.status?.capabilities?.unclaim === true;
  return `<div class="sec"><h4>Advanced tools</h4>
    <div class="row"><button class="btn sm" onclick="inspectDevice(${id})" ${ok ? '' : 'disabled'}>Inspect identity</button>
      ${t.capture ? `<button class="btn sm" onclick="finishCapture(${id},true)" ${t.busy ? 'disabled' : ''}>Stop and save recording</button><button class="btn sm" onclick="finishCapture(${id},false)" ${t.busy ? 'disabled' : ''}>Discard recording</button>`
        : `<button class="btn sm" onclick="captureDevice(${id})" ${ok ? '' : 'disabled'}>Record 30 seconds</button>`}
      ${removable ? `<button class="btn sm danger" onclick="removeDevice(${id})" ${ok && !t.removalUncertain ? '' : 'disabled'}>Remove from mesh…</button>` : ''}</div>
    ${t.message ? `<p role="status">${esc(t.message)}</p>` : ''}${t.review ? `<p>${opsReviewButton(t.receipt)}</p>` : ''}
    ${t.identity ? `<details open><summary>Identity details</summary><pre class="ops-pre">${esc(JSON.stringify(t.identity, null, 2))}</pre></details>` : ''}
    <p class="hint">Inspecting asks the device for its model and firmware. A recording saves the raw radio messages heard from it, for troubleshooting.${removable ? '' : ' Removing devices is switched off on this bridge.'}</p></div>`;
}

/* ---------- Accessory dimmer input mode (drawer) ---------- */
const OpsInput = new Map();
async function setAccessoryInput(id, enabled) {
  const v = S.controllerViews.find(x => x.device_id === id);
  if (!v || v.actions?.local_input?.enabled !== true || OpsInput.get(id)?.busy || !managementFresh()) return;
  if (!confirm(enabled
    ? 'Read this dimmer by the levels it reports?\n\nThis only changes how the bridge interprets the dimmer. It does not read, clear or change what the dimmer controls.'
    : 'Go back to reading this dimmer by the commands it sends?\n\nThis only changes how the bridge interprets the dimmer. It does not read, clear or change what the dimmer controls.')) return;
  OpsInput.set(id, {busy:true, message:'Changing…'}); renderSoon();
  try { await api('/api/controller/local-mode', {target:id, enabled}); OpsInput.set(id, {message:'Changed. The first report after a change is silent while the bridge finds the starting level.'}); }
  catch (e) { OpsInput.set(id, {message:'Not changed. ' + e.message}); }
  await refreshSnapshot(); renderSoon();
}
function accessoryInputHtml(d, v) {
  if (d.product_type !== 91 || !v) return '';
  const local = v.input_mode === 'local_level_reports', a = v.actions?.local_input, r = OpsInput.get(d.avion_id);
  const ok = a?.enabled === true && !r?.busy && managementFresh();
  return `<p>Input: ${local ? 'reported levels' : 'native commands'}${local && v.input_status === 'waiting_for_baseline' ? ' · waiting for the first report' : ''}.
      <button class="btn sm" onclick="setAccessoryInput(${d.avion_id},${!local})" ${ok ? '' : 'disabled'}>${local ? 'Use native commands' : 'Use reported levels'}</button></p>
    ${r?.message ? `<p role="status">${esc(r.message)}</p>` : ''}
    ${a && a.enabled !== true && a.reason === 'resolve_pending_operations' ? `<p class="hint">Review unfinished operations before changing this. ${opsReviewButton()}</p>` : ''}`;
}
