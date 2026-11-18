'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let session = null, preview = null, busy = false, finished = false, backupBusy = false;
  const choices = [...document.querySelectorAll('[data-setup-choice]')];
  let selection = null;
  function controls() {
    for (const button of choices) button.disabled = busy || finished || backupBusy;
    for (const id of ['email','password','location','login','preview','cancel','confirm','importConfirm']) $(id).disabled = busy || finished || backupBusy;
    $('create').disabled = busy || finished || backupBusy || !$('confirm').checked;
    $('import').disabled = busy || finished || backupBusy || !preview || !$('importConfirm').checked;
  }
  function status(text) { $('status').textContent = text; }
  async function post(path, data) {
    const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    const result = await r.json();
    if (!r.ok) { const error = new Error(result.message || 'The request could not be confirmed. Reload to check before trying again.'); error.code = result.error; throw error; }
    return result;
  }
  function reset() {
    session = preview = null; $('password').value = ''; $('importConfirm').checked = false;
    $('loginForm').hidden = false; $('locationStep').hidden = $('review').hidden = $('cancel').hidden = true;
    $('deviceList').replaceChildren(); $('groupList').replaceChildren(); $('location').replaceChildren();
  }
  async function run(work) {
    if (busy || finished || backupBusy) return;
    busy = true; controls(); window.BridgeBackup?.controls();
    try { await work(); } catch (e) { if (e.code === 'preview_expired') reset(); status(e.message); }
    finally { busy = false; controls(); window.BridgeBackup?.controls(); }
  }
  async function starting() {
    finished = true; controls(); status('Saved. Starting your local bridge…');
    for (let i=0;i<30;i++) {
      await new Promise(resolve => setTimeout(resolve,1000));
      try { const ready = await fetch('/api/capabilities'); if (ready.ok) { location.reload(); return; } } catch (_) {}
    }
    status('Setup is saved. Reload this page to open your bridge.');
  }
  async function choose(button) {
    if (selection?.dataset.setupChoice === 'cloud') {
      if (session) await post('/api/setup/cloud/cancel', {});
      reset();
      $('email').value = '';
    }
    if (selection?.dataset.setupChoice === 'restore') window.BridgeBackup?.discardDraft?.();
    $('confirm').checked = false;
    status('');
    selection = button;
    for (const option of choices) {
      const active = option === button;
      option.setAttribute('aria-expanded', String(active));
      $(option.getAttribute('aria-controls')).hidden = !active;
    }
    const heading = $(button.getAttribute('aria-controls')).querySelector('h2');
    if (heading) { heading.tabIndex = -1; heading.focus(); }
  }
  for (const button of choices) button.onclick = () => {
    if (selection !== button) run(() => choose(button));
  };
  $('confirm').onchange = controls;
  $('importConfirm').onchange = controls;
  $('location').onchange = () => { preview = null; $('review').hidden = true; $('importConfirm').checked = false; controls(); };
  $('loginForm').onsubmit = event => {
    event.preventDefault();
    if (busy || finished || backupBusy) return;
    let credentials = {email:$('email').value.trim(), password:$('password').value};
    $('password').value = '';
    run(async () => {
      status('Signing in and finding your locations…'); reset(); $('cancel').hidden = false;
      let request;
      try { request = post('/api/setup/cloud/login', credentials); } finally { credentials = null; }
      const data = await request; session = data.session;
      for (const row of data.locations) {
        const option = document.createElement('option'); option.value = row.id; option.textContent = row.name; $('location').append(option);
      }
      $('loginForm').hidden = true;
      if (!data.locations.length) { status('No locations were found in this account. Cancel to try another account.'); return; }
      $('locationStep').hidden = false; status('Choose the location to import, then preview its devices and groups.');
    });
  };
  $('preview').onclick = () => run(async () => {
    preview = null; $('review').hidden = true; $('importConfirm').checked = false;
    status('Loading your devices and groups…');
    const data = await post('/api/setup/cloud/preview', {session, location:$('location').value});
    preview = data.preview; $('deviceList').replaceChildren(); $('groupList').replaceChildren();
    const devices = data.inventory.devices, groups = data.inventory.groups;
    const names = new Map(devices.map(d => [d.device_id, d.name]));
    for (const d of devices) { const li = document.createElement('li'); li.textContent = d.name; $('deviceList').append(li); }
    for (const g of groups) { const li = document.createElement('li'); li.textContent = g.name + ' · ' + g.members.map(id => names.get(id)).join(', '); $('groupList').append(li); }
    $('reviewTitle').textContent = 'Import ' + data.location.name;
    $('summary').textContent = `${devices.length} device${devices.length === 1 ? '' : 's'} and ${groups.length} group${groups.length === 1 ? '' : 's'}. Existing pairing will be preserved.`;
    if (data.skipped_unaddressed) $('summary').textContent += ` ${data.skipped_unaddressed} unassigned cloud record(s) were left out because they have no mesh address.`;
    $('review').hidden = false; status('Review the setup, then confirm to import it for local control.');
  });
  $('cancel').onclick = () => run(async () => { await post('/api/setup/cloud/cancel', {}); reset(); status('Import cancelled. Nothing was saved.'); });
  $('import').onclick = () => run(async () => {
    if (!preview || !$('importConfirm').checked) return;
    status('Saving your existing network…');
    await post('/api/setup/cloud/apply', {session, preview, enable_local_control:true});
    reset(); await starting();
  });
  $('create').onclick = () => run(async () => {
    if (!$('confirm').checked) return;
    status('Creating your new network…'); await post('/api/setup', {create_new_mesh:true}); await starting();
  });
  window.BridgeBackup?.activate({setup:true,mount:$('setupBackupMount'),enabled:true,isBlocked:()=>busy || finished,onBusy:value=>{backupBusy=value;controls();},onRestoreAttempt:()=>{finished=true;controls();}});
  controls();
})();
