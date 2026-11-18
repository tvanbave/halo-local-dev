"use strict";
/* The same reviewed restore flow serves Settings and an empty replacement Pi.
   Passwords and uploaded bytes are never kept in storage or a retained draft. */
window.BridgeBackup = (() => {
  const MAX_UPLOAD_BYTES = 8 * 1024 * 1024 + 128 * 1024 + 128;
  const MAX_REQUEST_BYTES = Math.ceil(MAX_UPLOAD_BYTES / 3) * 4 + 16384;
  let panel = null, options = {}, status = null, loading = false, busy = false, attempted = false;
  let preview = null, expires = 0, expiryTimer = null, loadAttempted = false, finished = false;
  const field = id => panel?.querySelector('#' + id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const message = text => { if (field('backupResult')) field('backupResult').textContent = text; };
  const clearArchiveInputs = () => { for (const id of ['backupPassword','backupPasswordConfirm','restorePassword','restoreFile']) if (field(id)) field(id).value = ''; };
  function clearOwner() { if (field('backupOwnerPassword')) field('backupOwnerPassword').value = ''; }
  function clearSecrets() { clearArchiveInputs(); clearOwner(); }
  function ownerReady() {
    return !status?.owner_password_required || field('backupOwnerPassword').reportValidity();
  }
  const summaryRows = value => {
    const s = value || {}, count = key => Number.isInteger(s[key]) && s[key] >= 0 ? s[key] : '—';
    const date = typeof s.created_at === 'string' && Number.isFinite(Date.parse(s.created_at)) ? new Date(s.created_at).toLocaleString() : 'Not supplied';
    return `<dl class="backup-summary"><dt>Bridge</dt><dd>${escape(s.name || s.node || 'HALO Local')}</dd><dt>Created</dt><dd>${escape(date)}</dd><dt>Version</dt><dd>${escape(s.version || 'Unknown')}</dd><dt>Devices</dt><dd>${count('devices')}${Number.isInteger(s.controllers) ? ` (${s.controllers} controller${s.controllers === 1 ? '' : 's'})` : ''}</dd><dt>Groups</dt><dd>${count('groups')}</dd><dt>Scenes</dt><dd>${count('scenes')}</dd>${Number.isInteger(s.rooms) ? `<dt>Rooms</dt><dd>${count('rooms')}</dd>` : ''}${Number.isInteger(s.pending_operations) ? `<dt>Operations needing review</dt><dd>${count('pending_operations')}</dd>` : ''}</dl>`;
  };
  async function request(path, data) {
    const init = data === undefined ? {cache:'no-store'} : {method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)};
    if (data !== undefined && status?.owner_password_required) {
      const bytes = new TextEncoder().encode('halo:' + field('backupOwnerPassword').value);
      init.headers.Authorization = 'Basic ' + btoa(String.fromCharCode(...bytes));
    }
    const response = await fetch(path, init);
    const result = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error(result.message || 'The bridge could not confirm this request.'); error.status = response.status; error.code = result.error; throw error; }
    return result;
  }
  const knownFailures = new Set('invalid_backup invalid_backup_database invalid_backup_password invalid_backup_settings invalid_password_or_backup unsupported_backup_format different_installation different_mesh different_bridge invalid_admission invalid_backup_identity invalid_backup_time invalid_database invalid_inventory invalid_reservations invalid_restore_history invalid_rooms invalid_scenes invalid_state operation_history_changed physical_inventory_changed retired_device_in_inventory retired_transmitter sequence_exhausted superseded_installation unsupported_admission invalid_installation preview_expired confirmation_required setup_incomplete_preserved'.split(' '));
  function failure(error, fallback) {
    if (error?.code === 'backup_owner_required') return 'The bridge owner password was not accepted. Use the password for this Pi.';
    if (error?.status === 401 || error?.status === 403) return 'Access was refused. Open the bridge at its usual address and reload.';
    if ((/^(backup_|restore_|installation_backup_)/.test(error?.code || '') || knownFailures.has(error?.code)) && typeof error.message === 'string') return error.message;
    if (error?.status === 413) return 'That backup is too large for this bridge. Nothing was restored.';
    if (error?.status === 409) return 'The bridge or backup review changed. Reload and review the backup again before restoring.';
    if (error?.status === 400) return 'The backup or password was not accepted. Check both and try the preview again. Nothing was restored.';
    return fallback;
  }
  function build() {
    const element = document.createElement('section'); element.className = 'card backup-panel'; element.setAttribute('aria-label', options.setup ? 'Restore a backup' : 'Backup and restore');
    element.innerHTML = `${options.setup ? '<h2>Restore a HALO Local backup</h2>' : ''}
      <p>Keep your paired lights, names, rooms, groups, scenes, controller records and Home Assistant sharing. A full backup also includes your mesh and MQTT credentials, protected by your backup password.</p>
      <p class="hint">Home Assistant automations need a separate Home Assistant backup. Changes made after this backup are not included.</p>
      <p id="backupAvailability" role="status">Checking backup support…</p>
      <div class="field" id="backupOwnerField" hidden>
        <label for="backupOwnerPassword">This Pi’s bridge owner password</label>
        <input class="inp" id="backupOwnerPassword" type="password" maxlength="4096" autocomplete="off" aria-describedby="backupOwnerHelp">
        <p class="hint" id="backupOwnerHelp">Find this Pi’s password by running <code>docker compose exec bridge python -m avionmesh_pi login-info</code> on the Pi. The backup file has its own password.</p>
      </div>
      ${!options.setup ? `<form id="backupCreateForm" autocomplete="off"><fieldset id="backupCreateFields" disabled>
        <h4>Download a full backup</h4><p class="hint">Save the file somewhere other than this Pi. Keep its password separately; it cannot be recovered from the file.</p>
        <div class="field"><label for="backupPassword">New backup password</label><input class="inp" id="backupPassword" type="password" required minlength="12" maxlength="256" autocomplete="new-password" aria-describedby="backupPasswordHint"></div>
        <div class="field"><label for="backupPasswordConfirm">Confirm backup password</label><input class="inp" id="backupPasswordConfirm" type="password" required minlength="12" maxlength="256" autocomplete="new-password"></div>
        <p class="hint" id="backupPasswordHint">Use at least 12 characters. The file includes credentials needed to control your lights.</p>
        <button class="btn primary" id="backupCreate" type="submit">Download backup</button>
      </fieldset></form>` : ''}
      <form id="backupRestoreForm" autocomplete="off"><fieldset id="backupRestoreFields" disabled>
        ${!options.setup ? '<h4>Restore a backup</h4>' : ''}
        <p>Choose a full HALO Local backup to review before restoring. A device-list export is not a full backup.</p>
        <div class="field"><label for="restoreFile">Backup file</label><input class="inp" id="restoreFile" type="file" accept=".halobackup,.avionbackup,application/octet-stream" required aria-describedby="backupFileHint"></div>
        <p class="hint" id="backupFileHint">Choose the encrypted file downloaded from Backup & restore.</p>
        <div class="field"><label for="restorePassword">Backup password</label><input class="inp" id="restorePassword" type="password" required minlength="12" maxlength="256" autocomplete="off"></div>
        <button class="btn" id="backupPreview" type="submit">Review backup</button>
      </fieldset></form>
      <div id="backupReview"></div><p id="backupResult" role="status" aria-live="polite"></p>
      <button class="btn sm" id="backupRefresh" type="button">Refresh backup status</button>
      <button class="btn" id="backupReload" type="button" hidden>Reload bridge</button>`;
    return element;
  }
  function controls() {
    if (!panel) return;
    const blocked = busy || loading || finished || !!options.isBlocked?.();
    const ownerRequired = status?.owner_password_required === true;
    field('backupOwnerField').hidden = !ownerRequired;
    field('backupOwnerPassword').required = ownerRequired;
    field('backupOwnerPassword').disabled = blocked || !ownerRequired;
    if (field('backupCreateFields')) field('backupCreateFields').disabled = blocked || status?.enabled === false || !status?.backup_supported;
    field('backupRestoreFields').disabled = blocked || status?.enabled === false || !status?.restore_supported || attempted;
    field('backupRefresh').disabled = busy || loading || finished;
    const apply = field('backupApply');
    if (apply) apply.disabled = blocked || attempted || !preview?.can_restore || Date.now() >= expires ||
      !field('backupConfirmReplace')?.checked || !field('backupConfirmHistory')?.checked ||
      preview.mode === 'replacement' && !field('backupConfirmStopped')?.checked;
    if (field('backupDismiss')) field('backupDismiss').disabled = busy || finished;
  }
  function setBusy(value) { busy = value; options.onBusy?.(value); controls(); }
  async function load() {
    if (loading || busy || finished) return;
    loading = true; controls();
    try {
      status = await request('/api/backup/status');
      field('backupAvailability').textContent = status.enabled === false ? status.reason || 'Full backup and restore is unavailable on this installation.' :
        options.setup ? 'Restore here to use your existing paired lights on this Pi.' :
        status.last_backup?.created_at ? 'Last backup created: ' + new Date(status.last_backup.created_at).toLocaleString() + '. Keep a copy off this Pi.' : 'No backup has been created by this installation yet.';
    } catch (error) { status = null; field('backupAvailability').textContent = failure(error,'Could not load backup support. Refresh backup status to try again.'); }
    finally { loading = false; controls(); }
  }
  function dropPreview() {
    clearTimeout(expiryTimer); expiryTimer = null; preview = null; expires = 0;
    if (field('backupReview')) field('backupReview').replaceChildren();
  }
  function review(value) {
    if (!value || !['replacement','same_bridge'].includes(value.mode) || typeof value.can_restore !== 'boolean' ||
        value.can_restore && (typeof value.preview_id !== 'string' || !value.preview_id || typeof value.revision !== 'string' || !Number.isFinite(value.expires_in) || value.expires_in <= 0)) throw new Error('Invalid backup review');
    preview = value; expires = Date.now() + Math.max(0,value.expires_in || 0) * 1000;
    const blockers = Array.isArray(value.blockers) ? value.blockers : [], warnings = Array.isArray(value.warnings) ? value.warnings : [];
    const label = item => typeof item === 'string' ? item : item?.message || item?.reason || 'Review required.';
    field('backupReview').innerHTML = `<div class="backup-review"><h4>Review this backup</h4>${summaryRows(value.summary)}
      <p>${value.mode === 'replacement' ? 'This Pi will take over the backed-up bridge. Your lights stay paired.' : 'This bridge will stop and restart with the reviewed backup. Your lights stay paired.'}</p>
      ${warnings.length ? `<ul class="backup-notices">${warnings.map(item=>`<li>${escape(label(item))}</li>`).join('')}</ul>` : ''}
      ${blockers.length ? `<p><strong>Restore is unavailable:</strong></p><ul class="backup-notices">${blockers.map(item=>`<li>${escape(label(item))}</li>`).join('')}</ul>` : ''}
      <p class="hint">Restoring does not reset lights or reprogram groups, scenes or buttons. Light readings refresh after restart. Some controller or scene records may need a new read. Unfinished operations remain available for review.</p>
      ${value.can_restore ? `${value.mode === 'replacement' ? '<label class="backup-check"><input id="backupConfirmStopped" type="checkbox"> <span>The old bridge is stopped and will stay off.</span></label>' : ''}
      <label class="backup-check"><input id="backupConfirmReplace" type="checkbox"> <span>${value.mode === 'replacement' ? 'Restore this backup onto this Pi and start the restored bridge.' : 'Replace this bridge’s saved setup with this reviewed backup and restart it.'}</span></label>
      <label class="backup-check"><input id="backupConfirmHistory" type="checkbox"> <span>Changes made after this backup are not included. Restoring an older backup cannot verify later radio history.</span></label>
      <p class="hint">Home Assistant identifiers are preserved. Home Assistant automations are not included in this file.</p>
      <button class="btn primary" id="backupApply" type="button" disabled>Restore reviewed backup</button>` : ''}
      <button class="btn" id="backupDismiss" type="button">Discard review</button></div>`;
    for (const id of ['backupConfirmStopped','backupConfirmReplace','backupConfirmHistory']) if (field(id)) field(id).onchange = controls;
    if (field('backupApply')) field('backupApply').onclick = apply;
    field('backupDismiss').onclick = () => { clearSecrets(); dropPreview(); attempted = false; message('Backup review discarded. Nothing was restored.'); controls(); };
    if (value.can_restore) expiryTimer = setTimeout(() => { clearOwner(); message('This backup review expired. Choose the file and review it again. Nothing was restored.'); controls(); }, Math.min(2147483647,Math.max(0,expires-Date.now())+20));
    controls();
  }
  function fileBytes(file) {
    return new Promise((resolve,reject) => {
      const reader = new FileReader(); reader.onerror = () => reject(new Error('Could not read the selected file.'));
      reader.onload = () => resolve(String(reader.result).split(',')[1] || ''); reader.readAsDataURL(file);
    });
  }
  async function create() {
    if (busy || loading || finished || status?.enabled === false || !status?.backup_supported || options.isBlocked?.()) return;
    const password = field('backupPassword'), confirm = field('backupPasswordConfirm');
    confirm.setCustomValidity(confirm.value === password.value ? '' : 'The two passwords must match.');
    if (!ownerReady() || !field('backupCreateForm').reportValidity()) return;
    let data = {password:password.value}; clearArchiveInputs(); setBusy(true); message('Creating and checking your encrypted backup…');
    try {
      const result = await request('/api/backup/create',data); data.password = '';
      if (typeof result.archive !== 'string' || !result.archive || result.archive.length >= MAX_REQUEST_BYTES) throw new Error('Missing backup file');
      const decoded = atob(result.archive), chunks = [];
      for (let start=0;start<decoded.length;start+=65536) { const part=decoded.slice(start,start+65536), bytes=new Uint8Array(part.length);for(let i=0;i<part.length;i++)bytes[i]=part.charCodeAt(i);chunks.push(bytes); }
      const url = URL.createObjectURL(new Blob(chunks,{type:'application/octet-stream'})), link = document.createElement('a');
      const name = typeof result.filename === 'string' ? result.filename.split(/[\\/]/).pop().replace(/[\x00-\x1f\x7f]/g,'').slice(0,128) : '';
      link.href = url; link.download = name || 'halo-local-' + new Date().toISOString().slice(0,10) + '.halobackup'; document.body.append(link); link.click(); link.remove(); setTimeout(()=>URL.revokeObjectURL(url),60000);
      message('Encrypted backup created. Check your browser’s downloads and keep the file and password safe. No lights were reprogrammed.');
    } catch (error) { message(failure(error,'The backup download was not confirmed. Check your downloads before creating another backup. Nothing is retried automatically.')); }
    finally { data.password = ''; data = null; clearSecrets(); setBusy(false); }
  }
  async function previewFile() {
    if (busy || loading || finished || attempted || status?.enabled === false || !status?.restore_supported || options.isBlocked?.()) return;
    if (!ownerReady() || !field('backupRestoreForm').reportValidity()) return;
    const file = field('restoreFile').files?.[0]; if (!file) return;
    if (!file.size || file.size > MAX_UPLOAD_BYTES) { message('That file is empty or too large for this bridge. Nothing was uploaded or restored.'); clearSecrets(); return; }
    let data = {password:field('restorePassword').value,archive:''}; setBusy(true); dropPreview(); message('Opening and checking the backup. Nothing is being restored yet…');
    try {
      data.archive = await fileBytes(file); clearArchiveInputs();
      if (JSON.stringify(data).length >= MAX_REQUEST_BYTES) throw new Error('Backup too large');
      const result = await request('/api/backup/preview',data); data.password = data.archive = '';
      review(result); message(result.can_restore ? 'Review the backup and confirmations below. Nothing has been restored.' : 'This backup cannot currently be restored. Nothing was changed.');
    } catch (error) { dropPreview(); message(failure(error,'The backup review could not be completed. Choose the file and password again. Nothing was restored.')); }
    finally { data.password = data.archive = ''; data = null; clearArchiveInputs(); if (!preview?.can_restore) clearOwner(); setBusy(false); }
  }
  async function apply() {
    controls(); if (busy || finished || attempted || !preview?.can_restore || field('backupApply')?.disabled || !ownerReady()) return;
    const data = {preview_id:preview.preview_id,revision:preview.revision,confirm_old_bridge_stopped:true,confirm_replace:true,confirm_backup_history:true};
    attempted = true; setBusy(true); message('Restoring the reviewed backup. Keep this page open; do not repeat this request.');
    try {
      const result = await request('/api/backup/restore',data);
      if (result.accepted !== true) throw new Error('Restore not confirmed');
      finished = true; options.onRestoreAttempt?.(); dropPreview(); clearSecrets(); field('backupReload').hidden = false;
      message(result.restart_required ? 'Restore accepted. The bridge is restarting with the saved setup. Wait a moment, then reload the bridge.' : 'Restore accepted. Reload the bridge to see its saved setup.');
    } catch (error) {
      if (error.code === 'backup_owner_required') {
        attempted = false; message(failure(error)); return;
      }
      options.onRestoreAttempt?.();
      dropPreview(); field('backupReload').hidden = false;
      message(failure(error,'The restore response was not confirmed. The bridge may be restarting. Reload and check its setup before taking another action. Nothing will be retried automatically.'));
    } finally { clearSecrets(); setBusy(false); }
  }
  function activate(next = {}) {
    options = next;
    const mount = next.mount;
    if (!mount || next.enabled === false) { clearSecrets(); return; }
    if (!panel) {
      panel = build();
      if (field('backupCreateForm')) field('backupCreateForm').onsubmit = event => { event.preventDefault(); create(); };
      if (field('backupPasswordConfirm')) field('backupPasswordConfirm').oninput = () => field('backupPasswordConfirm').setCustomValidity('');
      if (field('backupPassword')) field('backupPassword').oninput = () => field('backupPasswordConfirm').setCustomValidity('');
      field('backupRestoreForm').onsubmit = event => { event.preventDefault(); previewFile(); };
      field('backupRefresh').onclick = () => { clearSecrets(); load(); };
      field('backupReload').onclick = () => location.reload();
    }
    if (panel.parentNode !== mount) mount.append(panel);
    if (!loadAttempted) { loadAttempted = true; load(); }
    controls();
  }
  function discardDraft() { if (!busy) { clearSecrets(); dropPreview(); message(''); controls(); } }
  return {activate,clearSecrets,discardDraft,controls,isBusy:()=>busy};
})();
