'use strict';
/* Local setup forms. Passwords live only in their input or the current request.
   The panel survives snapshot rerenders; navigation and every request clear them. */
window.BridgeSettings = (() => {
  let panel = null, mqtt = null, loading = false, mqttBusy = false, cloudBusy = false;
  let preview = null, previewTimer = null, mqttUncertain = false, initialLoadAttempted = false;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const field = id => panel?.querySelector('#' + id);
  const message = (id, text) => { if (field(id)) field(id).textContent = text; };
  const clearSecrets = () => { for (const id of ['mqttPassword','cloudPassword']) if (field(id)) field(id).value = ''; };
  const failure = (error, fallback) => {
    if ((/^(mqtt_|cloud_)/.test(error?.code || '') || error?.code === 'invalid_mqtt_settings') && typeof error.message === 'string') return error.message;
    if (error?.status === 401 || error?.status === 403) return 'Access was refused. Open the bridge at its usual address and reload.';
    if (error?.status === 409) return 'The settings or bridge state changed. Refresh and review before trying again.';
    if (error?.status === 400) return 'The bridge refused these details. Check the fields and try again.';
    return fallback;
  };
  function html() { return '<div id="bridgeSettingsMount" style="margin-bottom:14px"></div>'; }
  function build() {
    const element = document.createElement('div'); element.className = 'cards';
    element.innerHTML = `<details class="card settings-disclosure" data-settings-panel="mqtt">
      <summary id="mqttHeading">Home Assistant over MQTT</summary>
      <p>Connect this bridge to your MQTT broker. Lighting and wall controls work locally when Home Assistant is offline.</p>
      <p id="mqttConnection" role="status">Loading broker settings…</p>
      <form id="mqttForm" autocomplete="off"><fieldset id="mqttFields" disabled style="border:0;padding:0;margin:0;min-width:0">
        <p><label><input id="mqttEnabled" type="checkbox"> Enable MQTT</label></p>
        <div style="display:grid;grid-template-columns:minmax(0,3fr) minmax(78px,1fr);gap:10px">
          <div class="field"><label for="mqttHost">Broker address</label><input class="inp" id="mqttHost" type="text" maxlength="253" placeholder="192.168.1.10" spellcheck="false" autocapitalize="none" autocorrect="off"></div>
          <div class="field"><label for="mqttPort">Port</label><input class="inp" id="mqttPort" type="number" required min="1" max="65535" step="1" inputmode="numeric"></div>
        </div>
        <div class="field"><label for="mqttUsername">Username (optional)</label><input class="inp" id="mqttUsername" type="text" maxlength="256" spellcheck="false" autocapitalize="none" autocorrect="off"></div>
        <div class="field"><label for="mqttPasswordAction">Broker password</label><select class="inp" id="mqttPasswordAction"><option value="keep">Keep saved password</option><option value="replace">Set a new password</option><option value="clear">Use no password</option></select></div>
        <div class="field"><label for="mqttPassword">New password</label><input class="inp" id="mqttPassword" type="password" maxlength="4096" autocomplete="new-password" aria-describedby="mqttPasswordHelp"><small class="muted" id="mqttPasswordHelp">A saved password is never shown here.</small></div>
        <p><label><input id="mqttTls" type="checkbox"> Use TLS encryption</label></p>
        <p class="hint" id="mqttTlsHelp"></p>
        <div class="row"><button class="btn" id="mqttTest" type="button">Test connection</button><button class="btn primary" id="mqttSave" type="submit">Save MQTT settings</button></div>
      </fieldset></form>
      <p id="mqttResult" role="status" aria-live="polite"></p>
      <p class="hint">After connecting, open a device or group and choose Add to Home Assistant. Connecting a broker does not automatically share every light.</p>
      <button class="btn sm" id="mqttRefresh" type="button">Refresh saved settings</button>
    </details>
    <details class="card settings-disclosure" data-settings-panel="cloud">
      <summary id="cloudHeading">Import from Avi-on / HALO Home</summary>
      <p>Review devices and groups from your cloud account, then choose whether to import them. Cloud access is only used for this request.</p>
      <p class="hint">The current mesh is kept. Import does not pair lights, change wall-button assignments or program physical group membership. HALO account compatibility has not yet been verified.</p>
      <form id="cloudForm" autocomplete="off"><fieldset id="cloudFields" style="border:0;padding:0;margin:0;min-width:0">
        <div class="field"><label for="cloudEmail">Cloud account email</label><input class="inp" id="cloudEmail" type="email" maxlength="320" required autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false"></div>
        <div class="field"><label for="cloudPassword">Cloud account password</label><input class="inp" id="cloudPassword" type="password" maxlength="4096" required autocomplete="current-password"></div>
        <div class="field" id="cloudLocationField" hidden><label for="cloudLocation">Location</label><select class="inp" id="cloudLocation"><option value="">Choose a location</option></select></div>
        <button class="btn" id="cloudPreview" type="submit">Preview cloud import</button>
      </fieldset></form>
      <p id="cloudResult" role="status" aria-live="polite"></p>
      <div id="cloudReview"></div>
    </details>
    <details class="card settings-disclosure" data-settings-panel="updates" id="bridgeUpdatePanel" hidden>
      <summary>Bridge updates</summary>
      <p id="updateVersions"></p><p id="updateNotes" hidden><a target="_blank" rel="noopener noreferrer">View release notes on GitHub</a></p><p role="status" aria-live="polite" id="updateResult"></p>
      <p id="updateSetup" class="hint" hidden>To enable installation, run <code>sh tools/enable-updates.sh</code> once in the bridge folder on the Pi. For the bundled Bluetooth stack, append <code>compose.bluetooth.yaml</code>.</p>
      <div class="row"><button class="btn" id="updateCheck" type="button">Check for updates</button>
      <button class="btn primary" id="updateInstall" type="button" hidden>Update bridge</button></div>
      <div id="updateConfirm" hidden><p id="updatePrompt"></p><div class="row">
      <button class="btn" id="updateCancel" type="button">Cancel</button>
      <button class="btn primary" id="updateProceed" type="button">Install and restart</button></div></div>
    </details>
    <details class="card settings-disclosure" data-settings-panel="advanced" id="relayPanel" hidden><summary>Advanced</summary>
      <h3>Bluetooth relay</h3>
      <p id="relayCurrent">Loading current relay…</p>
      <div id="relayControls" hidden>
        <div class="field"><label for="relayMode">Relay selection</label><select class="inp" id="relayMode">
          <option value="automatic">Automatic</option><option value="preferred">Prefer a light</option></select></div>
        <div class="field" id="relayLightField" hidden><label for="relayLight">Preferred light</label><select class="inp" id="relayLight"></select></div>
        <p class="hint">On the next connection the bridge tries your preferred light first, then falls back automatically. Reconnect now to switch sooner.</p>
        <div class="row"><button type="button" class="btn primary" id="relaySave" disabled>Save preference</button>
          <button type="button" class="btn" id="relayReconnect">Reconnect now</button></div>
      </div>
      <h4>Relay candidates</h4>
      <p id="relayScanTime" class="hint"></p>
      <div class="row"><button type="button" class="btn" id="relayScan">Scan now</button>
        <button type="button" class="btn" id="relayExtended">Extended scan (60 seconds)</button></div>
      <p id="relayScanProgress" class="hint"></p>
      <ul id="relayList" class="relay-list"></ul>
      <section id="relayControllerSection" hidden><h4>Nearby controllers</h4><ul id="relayControllers" class="relay-list"></ul></section>
      <p id="relayResult" role="status" aria-live="polite"></p>
    </details>
    <details class="card settings-disclosure" data-settings-panel="backup" hidden><summary>Backup &amp; restore</summary><div id="backupSettingsMount"></div></details>`;
    return element;
  }
  function updatePassword() {
    if (!panel) return;
    const action = field('mqttPasswordAction').value;
    field('mqttPassword').disabled = action !== 'replace';
    field('mqttPassword').required = action === 'replace' && field('mqttEnabled').checked;
    if (action !== 'replace') field('mqttPassword').value = '';
    message('mqttPasswordHelp', mqtt?.password_set ? 'A password is saved. Choose a new password only to replace it.' : 'No password is saved. Choose “Set a new password” if your broker requires one.');
  }
  function updateTls() {
    message('mqttTlsHelp', field('mqttTls').checked ? 'The broker certificate is verified. Use the hostname shown on its certificate.' : 'The connection is unencrypted. Use this only with your trusted local broker.');
  }
  function connectionText() {
    if (!mqtt) return 'Broker settings are unavailable.';
    return mqtt.enabled ? (mqtt.connected ? 'MQTT broker connected. Home Assistant receipt is not verified here.' : 'MQTT is enabled; the broker is not connected.') : 'MQTT is disabled.';
  }
  function showPublic(value, overwrite = true) {
    if (!value || typeof value.revision !== 'string') throw new Error('Missing settings revision');
    mqtt = {revision:value.revision,enabled:value.enabled === true,host:value.host || '',port:value.port || (value.tls === false ? 1883 : 8883),username:value.username || '',tls:value.tls !== false,password_set:value.password_set === true,connected:value.connected === true,source:value.source};
    if (overwrite) {
      field('mqttEnabled').checked = mqtt.enabled; field('mqttHost').value = mqtt.host;
      field('mqttPort').value = mqtt.port; field('mqttUsername').value = mqtt.username; field('mqttTls').checked = mqtt.tls;
      field('mqttPasswordAction').value = mqtt.password_set ? 'keep' : 'clear'; field('mqttPassword').value = '';
    }
    field('mqttPasswordAction').querySelector('[value="keep"]').disabled = !mqtt.password_set;
    message('mqttConnection', connectionText()); updatePassword(); updateTls();
  }
  function busyMqtt(value) {
    mqttBusy = value; field('mqttFields').disabled = value || !mqtt || mqttUncertain;
    field('mqttRefresh').disabled = value || loading;
  }
  async function loadMqtt(overwrite = true) {
    if (loading || mqttBusy) return;
    loading = true; field('mqttRefresh').disabled = true;
    try {
      showPublic(await api('/api/settings/mqtt'), overwrite); mqttUncertain = false;
      message('mqttResult', overwrite ? 'Saved settings loaded.' : 'Saved settings refreshed. Your unsaved fields are preserved; review them before saving.');
    } catch (error) { message('mqttResult', failure(error, 'Could not load saved settings. Refresh to try again.')); }
    finally { loading = false; busyMqtt(false); }
  }
  function mqttPayload(test) {
    const form = field('mqttForm'), enabled = field('mqttEnabled').checked;
    field('mqttHost').required = enabled || test;
    field('mqttPassword').required = field('mqttPasswordAction').value === 'replace' && (enabled || test);
    if (!form.reportValidity()) return null;
    const data = {revision:mqtt.revision,enabled:test || enabled,host:field('mqttHost').value.trim(),port:Number(field('mqttPort').value),username:field('mqttUsername').value.trim(),tls:field('mqttTls').checked,password_action:field('mqttPasswordAction').value};
    if (data.password_action === 'replace') data.password = field('mqttPassword').value;
    return data;
  }
  async function submitMqtt(test) {
    if (mqttBusy || loading || !mqtt || mqttUncertain) return;
    const data = mqttPayload(test); if (!data) return;
    const replaced = data.password_action === 'replace'; busyMqtt(true);
    message('mqttResult', test ? 'Checking broker connection…' : 'Saving MQTT settings…');
    try {
      const result = await api(test ? '/api/settings/mqtt/test' : '/api/settings/mqtt', data);
      if (test) {
        message('mqttResult', result.connected === true ? 'Broker connection succeeded. This does not confirm Home Assistant discovery.' + (replaced ? ' Re-enter the new password to save it.' : ' Settings have not been saved.') : 'The broker connection was not confirmed. Check the broker details before saving.');
      } else {
        showPublic(result.settings || result);
        if (result.durability_confirmed === false) {
          mqttUncertain = true;
          message('mqttResult', 'MQTT settings were applied, but saving them durably could not be confirmed. Refresh and review before another change. They may not survive a restart.');
        } else {
          message('mqttResult', 'MQTT settings saved. ' + connectionText());
        }
        if (typeof refreshSnapshot === 'function') await refreshSnapshot();
      }
    } catch (error) {
      if (!test && ![400,401,403,409].includes(error?.status)) mqttUncertain = true;
      message('mqttResult', failure(error, test ? 'Could not confirm the broker connection. Nothing was saved.' : 'The save response was lost. Refresh saved settings before another change. Nothing is retried automatically.'));
    } finally { delete data.password; field('mqttPassword').value = ''; busyMqtt(false); }
  }
  function discardPreview() {
    clearTimeout(previewTimer); previewTimer = null; preview = null;
    if (field('cloudReview')) field('cloudReview').replaceChildren();
  }
  function previewCount(value) {
    const c = value?.changes || {};
    return (c.devices_added || []).length + (c.groups_added || []).length + (c.members_added || []).length;
  }
  function renderPreview() {
    const area = field('cloudReview'); area.replaceChildren(); if (!preview) return;
    const p = preview, c = p.changes || {}, expired = p.expires_at && Date.now() >= p.expires_at;
    const location = p.selected_location;
    const lists = [
      ['Devices to add', c.devices_added || [], row => `${row.name} · #${row.device_id}`],
      ['Groups to add', c.groups_added || [], row => `${row.name} · ${(row.members || []).length} members`],
      ['Existing groups to extend in the inventory', c.members_added || [], row => `${row.name} · ${(row.members || []).length} additional members`],
    ];
    area.innerHTML = `<div class="panel" style="padding:12px;margin-top:12px;overflow-wrap:anywhere">
      <h4 style="margin:0 0 8px">${location ? escape(location.name) + ' — import review' : 'Import review'}</h4>
      ${location ? `<p>${location.matching_mesh ? 'This location matches the bridge’s mesh.' : 'This location uses a different mesh. Its devices cannot be added to the current mesh by importing metadata.'}</p>` : ''}
      ${lists.map(([title, rows, label]) => rows.length ? `<p><strong>${escape(title)} (${rows.length})</strong></p><ul>${rows.map(row => `<li>${escape(label(row))}</li>`).join('')}</ul>` : '').join('')}
      ${p.can_apply && !previewCount(p) ? '<p>There is no new metadata to import.</p>' : ''}
      <p>${escape(expired ? 'This preview expired. Request a new preview before importing.' : p.message || '')}</p>
      <p class="hint">Existing names and Home Assistant sharing are preserved. New devices remain unshared.</p>
      <div class="row"><button class="btn primary" id="cloudApply" type="button" ${cloudBusy || !p.can_apply || expired || !previewCount(p) ? 'disabled' : ''}>Import reviewed metadata</button><button class="btn" id="cloudDismiss" type="button" ${cloudBusy ? 'disabled' : ''}>Discard preview</button></div>
    </div>`;
    field('cloudApply').onclick = applyCloud;
    field('cloudDismiss').onclick = () => { discardPreview(); message('cloudResult', 'Preview discarded. Nothing was imported.'); };
  }
  function busyCloud(value) { cloudBusy = value; field('cloudFields').disabled = value; renderPreview(); }
  async function previewCloud() {
    if (cloudBusy || !field('cloudForm').reportValidity()) return;
    const data = {email:field('cloudEmail').value.trim(),password:field('cloudPassword').value};
    if (field('cloudLocation').value) data.location_id = field('cloudLocation').value;
    discardPreview(); busyCloud(true); message('cloudResult', 'Reading the cloud account. No local changes are being made…');
    try {
      const result = await api('/api/cloud-import/preview', data);
      const selected = result.selected_location?.id || data.location_id || '';
      field('cloudLocation').innerHTML = '<option value="">Choose a location</option>' + (result.locations || []).map(location => `<option value="${escape(location.id)}" ${location.id === selected ? 'selected' : ''}>${escape(location.name)}${location.matching_mesh ? ' — current mesh' : ' — different mesh'}</option>`).join('');
      field('cloudLocationField').hidden = !(result.locations || []).length;
      if (result.status === 'select_location') {
        message('cloudResult', 'Choose a location, then re-enter your account credentials to preview it. Nothing has been saved.');
      } else {
        preview = {...result, expires_at: result.expires_in ? Date.now() + result.expires_in * 1000 : null};
        if (preview.expires_at) previewTimer = setTimeout(renderPreview, Math.max(0, preview.expires_at - Date.now()) + 10);
        message('cloudResult', result.can_apply ? 'Review the changes below. Nothing has been imported yet.' : result.message || 'This location cannot be imported into the current mesh.');
      }
    } catch (error) { message('cloudResult', failure(error, 'Could not preview the cloud account. Nothing was imported. Check the account details and try again.')); }
    finally { delete data.password; delete data.email; field('cloudPassword').value = ''; field('cloudEmail').value = ''; busyCloud(false); }
  }
  async function applyCloud() {
    if (cloudBusy || !preview?.can_apply || !preview.preview_id || !previewCount(preview)) return;
    if (preview.expires_at && Date.now() >= preview.expires_at) return renderPreview();
    if (!window.confirm('Import the reviewed device and group metadata? This keeps the current mesh, existing names and Home Assistant sharing. It does not change physical memberships.')) return;
    const data = {preview_id:preview.preview_id,revision:preview.revision};
    preview.can_apply = false; busyCloud(true); message('cloudResult', 'Importing the reviewed metadata…');
    try {
      const result = await api('/api/cloud-import/apply', data);
      if (result.saved !== true) throw new Error('Import not confirmed');
      discardPreview(); message('cloudResult', 'Reviewed metadata imported. New devices are not shared with Home Assistant.');
      if (typeof refreshSnapshot === 'function') await refreshSnapshot();
    } catch (error) {
      discardPreview(); message('cloudResult', failure(error, 'The import response was not confirmed. Refresh the inventory and request a new preview before another change. Nothing is retried automatically.'));
    } finally { busyCloud(false); }
  }
  let updateState = null, updateTimer = null, updateBusy = false, reviewedVersion = null;
  const updating = phase => ['preparing','ready','installing','rolling_back'].includes(phase);
  const updateVisible = () => panel?.isConnected && field('bridgeUpdatePanel')?.open;
  function drawUpdate(value) {
    if (typeof value?.installed !== 'string' || typeof value.job?.phase !== 'string') throw new Error('Waiting for bridge update status.');
    updateState = value;
    const job = value.job || {}, active = updating(job.phase);
    message('updateVersions', 'Installed ' + value.installed + (value.latest ? ' · Latest ' + value.latest : ''));
    const notes = field('updateNotes');
    notes.hidden = !value.latest;
    if (value.latest) notes.querySelector('a').href = 'https://github.com/tvanbave/halo-local-dev/releases/tag/v' + encodeURIComponent(value.latest.replace('.dev','-dev'));
    const checked = value.latest && !value.available ? (value.latest === value.installed ? 'Your bridge is up to date.' : 'Your installed build is newer than the public release.') : '';
    message('updateResult', job.message || checked || (value.available ? 'A newer public build is available.' : 'Check GitHub for a public build.'));
    field('updateSetup').hidden = job.phase !== 'unavailable';
    field('updateCheck').disabled = active;
    field('updateInstall').hidden = !value.available || active || ['needs_review','unavailable'].includes(job.phase);
    field('updateInstall').textContent = 'Update to ' + (value.latest || 'latest');
    if (active || reviewedVersion !== value.latest) cancelUpdate();
    if (active) scheduleUpdate();
  }
  function cancelUpdate() { reviewedVersion = null; field('updateConfirm').hidden = true; }
  function scheduleUpdate() {
    clearTimeout(updateTimer);
    if (updateVisible()) updateTimer = setTimeout(() => updateRequest('status'), 4000);
  }
  async function updateRequest(action) {
    if (updateBusy) return;
    updateBusy = true;
    clearTimeout(updateTimer);
    const version = reviewedVersion;
    cancelUpdate();
    field('updateCheck').disabled = field('updateInstall').disabled = true;
    if (action !== 'status') message('updateResult', action === 'check' ? 'Checking GitHub…' : 'Starting the update…');
    try {
      const base = '/api/settings/update';
      const value = action === 'status' ? await api(base) : await api(base + '/' + action,
        action === 'check' ? {} : {version, confirm_restart:true});
      drawUpdate(value);
    } catch (error) {
      const reconnect = action !== 'check' && !error.status;
      message('updateResult', reconnect ? 'Waiting to reconnect to the bridge…' : error.message || 'Could not reach the bridge.');
      field('updateInstall').hidden = true;
      field('updateCheck').disabled = false;
      if (reconnect || action === 'install') scheduleUpdate();
    } finally {
      updateBusy = false;
      field('updateInstall').disabled = false;
    }
  }
  let relay = null, relayBusy = false, relayDirty = false, relayRevision = null;
  let relayOptions = null, relayStamp = null;
  const relayVisible = () => panel?.isConnected && field('relayPanel')?.open;
  const sampleTime = value => new Date(value * 1000).toLocaleString();
  function relayButtons() {
    const prefer = field('relayMode').value === 'preferred';
    field('relayLightField').hidden = !prefer;
    field('relaySave').disabled = !relay?.configurable || relayBusy || !relayDirty || (prefer && !field('relayLight').value);
    field('relayMode').disabled = field('relayLight').disabled = !relay || relayBusy === 'save' || relayBusy === 'reconnect';
    const scanning = relay?.extended_scan?.phase === 'running';
    field('relayExtended').disabled = field('relayScan').disabled = !!relayBusy || scanning || !relay?.configurable;
    field('relayReconnect').disabled = scanning || relayBusy || relayDirty || !relay?.current;
  }
  function meshReport(row) {
    if (row.device_id == null) return 'Identity unknown';
    const state = typeof S === 'undefined' ? {} : S;
    if (state.status?.mesh_ready === false) return 'Bridge disconnected';
    if (row.kind === 'controller') {
      return state.controllerViews?.find(v => v.device_id === row.device_id)?.last_event
        ? 'Input observed this connection' : 'Waiting for controller input';
    }
    const light = state.devices?.find(d => d.avion_id === row.device_id);
    // The connected relay's own reports reach the Pi over the connection itself, not through other lights.
    if (light?.power_fresh || light?.color_temp_fresh)
      return row.address === relay?.current ? 'Reporting directly (connected relay)' : 'Reporting through mesh';
    if (light?.power_read_status === 'no_reply') return 'Recent power checks unanswered';
    if (light?.power_last_known || light?.color_temp_last_known || light?.power_observed_ms != null || light?.color_temp != null)
      return 'Earlier mesh report';
    return 'Waiting for mesh report';
  }
  function showRelayRows(id, rows) {
    const list = field(id);
    const html = rows.map(row => '<li><strong>' + escape(row.name || 'Unidentified relay') +
      '</strong> · ' + escape(row.address) +
      (row.identity === 'observed' ? '<br>Identified while connected as the relay' : '') + '<br>Direct signal: ' +
      (row.rssi == null ? 'No signal reading yet' : escape(row.rssi)+' dBm · '+escape(sampleTime(row.seen_at)) +
      (row.latest ? '' : ' · Not seen in latest scan')) + '<br>Mesh: ' + meshReport(row) + '</li>').join('');
    if (list._relayMarkup !== html) { list.innerHTML = html; list._relayMarkup = html; }
  }
  function updateRelayReadings() {
    if (!relay) return;
    const sorted = rows => [...rows].sort((a,b) => (a.name || a.address).localeCompare(b.name || b.address, undefined, {numeric:true}));
    showRelayRows('relayList', sorted(relay.candidates));
    const controllers = relay.controllers || [];
    field('relayControllerSection').hidden = !controllers.length;
    showRelayRows('relayControllers', sorted(controllers));
    const scan = relay.extended_scan;
    const latest = typeof S !== 'undefined' && S.status?.relay_extended_scan;
    const remaining = latest?.phase === 'running' ? latest.remaining_seconds : scan?.remaining_seconds;
    const phases = {complete:'Extended scan complete.', interrupted:'Extended scan interrupted. Previous readings are shown.',
      failed:'Extended scan failed. Previous readings are shown.'};
    message('relayScanProgress', scan?.phase === 'running' ? 'Extended scan in progress · ' + remaining + ' seconds remaining. Lights are available.' : phases[scan?.phase] || '');
  }
  function drawRelay(value) {
    relay = value;
    const rows = [...value.candidates].sort((a,b) => (a.name || a.address).localeCompare(b.name || b.address, undefined, {numeric:true}));
    const connected = [...rows, ...(value.controllers || [])].find(row => row.address === value.current);
    message('relayCurrent', connected ? 'Current relay: ' + (connected.name || connected.address) +
      (value.fallback ? ' (automatic fallback)' : '') : 'Bluetooth relay disconnected');
    field('relayControls').hidden = !value.configurable;
    const select = field('relayLight'), options = rows.filter(row => row.device_id !== null);
    const optionsHtml = '<option value="">Choose a paired light</option>' + options.map(row =>
      '<option value="'+escape(row.address)+'">'+escape(row.name)+' (#'+escape(row.device_id)+')</option>').join('');
    if (relayOptions !== optionsHtml) {
      const previous = select.value;
      select.innerHTML = relayOptions = optionsHtml;
      select.value = previous || '';
    }
    if (!relayDirty) {
      relayRevision = value.revision;
      field('relayMode').value = value.preferred ? 'preferred' : 'automatic';
      select.value = value.preferred || '';
    }
    updateRelayReadings();
    message('relayScanTime', !value.configurable ? 'Relay selection is fixed by the installation configuration.' :
      (value.scanned_at ? 'Last scan: '+sampleTime(value.scanned_at)+'. ' : '') +
      'Direct signal measures advertisements heard at the Pi. A light can report through the mesh even when the Pi hears no advertisement.');
    field('relayExtended').hidden = field('relayScan').hidden = !value.configurable;
    relayButtons();
  }
  async function relayRequest(action) {
    if (relayBusy || !relayVisible()) return;
    let url = '/api/settings/relay', data;
    if (action === 'save') {
      const row = relay?.candidates.find(item => item.address === field('relayLight').value && item.device_id !== null);
      const prefer = field('relayMode').value === 'preferred';
      if (!relayDirty || (prefer && !row)) return;
      data = {revision:relayRevision, preferred:prefer ? {address:row.address,device_id:row.device_id} : null};
    } else if (action === 'scan' || action === 'extended') {
      url += '/scan'; data = action === 'extended' ? {seconds:60} : {};
    } else if (action === 'reconnect') {
      if (!relay?.current || relayDirty) return;
      url += '/reconnect'; data = {revision:relay.revision,current:relay.current};
    }
    relayBusy = action; relayButtons();
    if (action === 'extended') message('relayResult','');
    if (action === 'scan') message('relayResult','Scanning for about five seconds…');
    try {
      const value = await api(url,data);
      if (action === 'save') relayDirty = false;
      drawRelay(value);
      if (action === 'save') message('relayResult','Preference saved for the next connection.');
      if (action === 'scan') {
        const count = value.candidates.filter(row => row.latest).length;
        message('relayResult',count ? 'Scan complete. Heard '+count+(count===1?' candidate.':' candidates.') : 'Scan complete. No relay advertisements heard.');
      }
      if (action === 'reconnect') message('relayResult','Reconnecting using the saved preference…');
      if (action === 'status' && value.current && field('relayResult').textContent.startsWith('Reconnecting')) message('relayResult','Bluetooth reconnected.');
    } catch (error) {
      const fallback = action === 'status' ? 'Could not load relay information. Open Advanced again to refresh.' :
        'The bridge did not confirm the request. Open Advanced again to check its status.';
      message('relayResult', ['relay_unavailable','relay_scan_failed'].includes(error?.code) ? error.message : failure(error,fallback));
      if (action === 'save' && error.status === 409) {
        relayDirty = false;
        try { drawRelay(await api('/api/settings/relay')); } catch { relay = null; }
      }
    } finally { relayBusy = false; relayButtons(); }
  }
  function activate() {
    const mount = document.getElementById('bridgeSettingsMount');
    if (!mount) { clearSecrets(); clearTimeout(updateTimer); return; }
    if (!panel) {
      panel = build();
      field('relayPanel').ontoggle = () => { if (relayVisible()) relayRequest('status'); };
      const editRelay = () => { relayDirty = true; message('relayResult',''); relayButtons(); };
      field('relayMode').onchange = field('relayLight').onchange = editRelay;
      field('relaySave').onclick = () => relayRequest('save');
      field('relayExtended').onclick = () => relayRequest('extended');
      field('relayScan').onclick = () => relayRequest('scan');
      field('relayReconnect').onclick = () => relayRequest('reconnect');
      field('bridgeUpdatePanel').ontoggle = () => {
        if (updateVisible()) updateRequest('status');
        else { cancelUpdate(); clearTimeout(updateTimer); }
      };
      field('updateCheck').onclick = () => updateRequest('check');
      field('updateInstall').onclick = () => {
        reviewedVersion = updateState.latest;
        message('updatePrompt', 'Install ' + reviewedVersion + '? The bridge will restart briefly. Your pairings and settings are kept.');
        field('updateConfirm').hidden = false;
      };
      field('updateCancel').onclick = cancelUpdate;
      field('updateProceed').onclick = () => { if (reviewedVersion) updateRequest('install'); };
      const backupDisclosure = field('backupSettingsMount')?.parentElement;
      if (backupDisclosure) backupDisclosure.addEventListener('toggle', () => {
        if (!backupDisclosure.open && !window.BridgeBackup?.isBusy()) window.BridgeBackup?.discardDraft?.();
      });
      field('mqttForm').onsubmit = event => { event.preventDefault(); submitMqtt(false); };
      field('mqttTest').onclick = () => submitMqtt(true);
      field('mqttRefresh').onclick = () => { clearSecrets(); loadMqtt(false); };
      field('mqttPasswordAction').onchange = updatePassword;
      field('mqttEnabled').onchange = updatePassword;
      field('mqttTls').onchange = () => { const current = Number(field('mqttPort').value); if ([1883,8883].includes(current)) field('mqttPort').value = field('mqttTls').checked ? 8883 : 1883; updateTls(); };
      field('cloudForm').onsubmit = event => { event.preventDefault(); previewCloud(); };
    }
    if (panel.parentNode !== mount) {
      mount.replaceChildren(panel);
      if (updateVisible()) updateRequest('status');
    }
    field('relayPanel').hidden = typeof feat !== 'function' || !feat('relay_settings');
    if (relayVisible() && typeof S !== 'undefined' && !relayBusy) {
      updateRelayReadings();
      const stamp = [S.status?.relay_extended_scan?.phase,S.status?.instance,S.status?.inventory_revision,S.status?.mesh_ready,S.status?.relay_scan_number,S.status?.relay_identity].join('|');
      if (stamp !== relayStamp) { relayStamp = stamp; relayRequest('status'); }
    }
    field('bridgeUpdatePanel').hidden = typeof feat !== 'function' || !feat('software_updates');
    const backupMount = field('backupSettingsMount');
    if (backupMount?.parentElement) backupMount.parentElement.hidden = typeof feat !== 'function' || !feat('backup');
    if (!mqtt && !loading && !initialLoadAttempted) { initialLoadAttempted = true; loadMqtt(); }
    if (mqtt && typeof S !== 'undefined' && S.status?.mqtt_enabled === mqtt.enabled) {
      mqtt.connected = S.status.mqtt_ready === true; message('mqttConnection', connectionText());
    }
  }
  return {html, activate};
})();
