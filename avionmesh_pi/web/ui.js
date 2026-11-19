'use strict';
/* HALO Local bridge UI for the pi-avionmesh bridge (trusted-LAN home page).
   Ported from the HALO Local redesign. Rules:
   - Bluetooth work runs one job at a time. Only idempotent work (light commands, reads, searches)
     waits and retries while the bridge is busy. Claims and metadata edits are never resent.
   - State shown is only what devices reported (snapshot device_states), never what was requested.
   - Failed claims are never retried automatically. The bridge keeps a receipt of each attempt;
     review and recovery are in Operations (ops.js), reached from Settings.
   - Features the bridge doesn't report in /api/capabilities are hidden, not shown as errors. */

/* ops.js normally defines these. The fallbacks keep the rest of the page working if it fails to load. */
for (const name of ['opsReviewButton', 'deviceToolsHtml', 'accessoryInputHtml', 'opsSettingsCards', 'opsInventoryCard'])
  if (typeof globalThis[name] !== 'function') globalThis[name] = () => '';
if (typeof globalThis.viewOperations !== 'function')
  globalThis.viewOperations = () => '<div class="view-head"><div><h2>Operations</h2></div></div><p class="empty">Operations could not load. Reload the page.</p>';

const I = {
  home:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v9.5h13V10"/></svg>',
  list:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M8 6h12M8 12h12M8 18h12"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/></svg>',
  layers:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 13 9 5 9-5"/></svg>',
  plus:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  activity:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg>',
  gear:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/></svg>',
  keypad:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="6" y="3" width="12" height="18" rx="2"/><path d="M9 7h6M9 11h6M9 15h6"/></svg>',
  dimmer:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="6" y="3" width="12" height="18" rx="2"/><rect x="10" y="7" width="4" height="10" rx="1"/></svg>',
  bulb:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3.5 10.9c.6.4 1 1.1 1 1.8V16h5v-.3c0-.7.4-1.4 1-1.8A6 6 0 0 0 12 3Z"/></svg>',
  search:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  close:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5 9-10"/></svg>',
  alert:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3 2 20h20L12 3Z"/><path d="M12 10v4M12 17h.01"/></svg>',
  info:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>',
  flash:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/></svg>',
};

const BLE_NAMES = ['Idle','Scanning','Connecting','Discovering','Ready','Disconnected','Retrying'];
const TYPE_NAMES = {81:'Anyplace dimmer',90:'Lamp dimmer',91:'Accessory dimmer',93:'RL4 downlight',94:'Light adapter',97:'Smart dimmer',
  126:'Battery scene keypad',127:'Wired scene keypad',134:'A19 bulb',137:'Surface downlight',162:'Micro Edge',167:'Smart switch'};
const CONTROLLER_SUPPORT = {
  127:'Read and configure buttons from this controller’s details.',
  126:'Read buttons and follow the battery wake prompts in this controller’s details. Programming is separately gated.',
  91:'Set up Home Assistant control from this controller’s details.',
  81:'Check Anyplace input and review separately enabled experimental HA setup in this controller’s details.',
};
const NO_ROOM = 'No room yet';

const S = {
  devices:[], groups:[], synced:false, ble:0, rx:0, sse:false, health:null,
  caps:{bridge:'pi-avionmesh', features:{}, types:{}}, status:null, revision:null, roomMap:{},
  view:'home', drawer:null, filter:'all', q:'', controllerQ:'', actFilter:'all', showRaw:false,
  log:[], radio:{label:null, queue:[]}, pending:{},
  wiz:null, groupWorkflow:null, controllerViews:[], sceneWorkflow:null,
};

/* ---------- helpers ---------- */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const pct = b => Math.round(b / 255 * 100);
const raw = p => Math.round(p * 255 / 100);
const feat = k => S.caps.features && S.caps.features[k];
const lights = () => S.devices.filter(d => d.kind !== 'controller');
const controllers = () => S.devices.filter(d => d.kind === 'controller');
const dev = id => S.devices.find(d => d.avion_id === id);
const grp = id => S.groups.find(g => g.group_id === id);
// Keep internal event reservations in S.groups for controller ownership lookups.
const lightingGroups = () => S.groups.filter(g => !g.protected && g.group_id !== 24566);
const gName = id => (grp(id) || {name:'Group ' + id}).name;
const members = gid => (grp(gid)?.members || []).map(dev).filter(Boolean);
const byName = (a, b) => a.name.localeCompare(b.name, undefined, {numeric:true});
const known = d => d.brightness !== undefined;
const gettingStatus = d => !known(d) && (d.power_read_status === 'waiting' || d.power_read_status === 'retry_pending' ||
  (!['no_reply', 'unconfirmed'].includes(d.power_read_status) && Number.isFinite(d.initial_status_until) && Date.now() < d.initial_status_until));
const missingPowerReply = d => !known(d) && !gettingStatus(d) &&
  (d.power_read_status === 'no_reply' || d.power_read_status === undefined);
const isOn = d => known(d) && d.brightness > 0;
const canDim = d => d.kind === 'light' && d.has_dimming !== false && d.product_type !== 167;
function powerCounts(devices) {
  if (!devices.length) return 'No lights';
  const current = devices.filter(d => known(d) && d.power_fresh);
  if (!current.length) return `Waiting for light status · ${devices.length} awaiting readings`;
  const on = current.filter(isOn).length;
  return [on + ' on', (current.length - on) + ' off',
    current.length < devices.length ? (devices.length - current.length) + ' awaiting readings' : ''].filter(Boolean).join(' · ');
}
const roomOf = d => d.room || '';
const rooms = () => [...new Set(S.devices.map(roomOf).filter(Boolean))].sort((a, b) => a.localeCompare(b));
const typeName = d => TYPE_NAMES[d.product_type] || d.product_name || 'HALO device';
const timeNow = () => new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
const article = n => (/^[aeiou]/i.test(n) ? 'an ' : 'a ') + n;
function dupNames(list) { const c = {}; list.forEach(x => c[x.name] = (c[x.name] || 0) + 1); return list.filter(x => c[x.name] > 1); }
/* Readings are what a light last reported. Show their age once they are no longer recent,
   because a wall control or the HALO app may have changed the light since. */
const reportedAt = {};
function ageText(id) {
  const at = reportedAt[id]; if (!at) return '';
  const m = Math.floor((Date.now() - at) / 60000);
  if (m < 2) return '';
  return m < 60 ? ` · ${m} min ago` : m < 2880 ? ` · ${Math.floor(m / 60)} h ago` : ` · ${Math.floor(m / 1440)} days ago`;
}
function stateText(d) {
  if (S.pending[d.avion_id]) return 'Sent · waiting for reply';
  if (!known(d) && d.power_read_status === 'retry_pending') return 'Waiting for next check…';
  return !known(d) ? (gettingStatus(d) ? 'Getting status…' : d.power_read_status === 'no_reply' ? 'No power reading' : 'State unknown') : (d.power_last_known ? 'Last known: ' : '') + (isOn(d) ? (canDim(d) ? pct(d.brightness) + '%' : 'On') : 'Off') + ageText(d.avion_id);
}
function kelvinColor(k) {
  const stops = [[2700,[255,174,82]],[3000,[255,198,128]],[3500,[255,221,176]],[4000,[255,240,220]],[5000,[238,242,255]]];
  for (let i = 0; i < stops.length - 1; i++) { const [a, ca] = stops[i], [b, cb] = stops[i + 1];
    if (k <= b) { const t = Math.max(0, (k - a) / (b - a)); return 'rgb(' + ca.map((v, j) => Math.round(v + (cb[j] - v) * t)).join(',') + ')'; } }
  return 'rgb(238,242,255)';
}
function glow(d, button = true) {
  const on = devicePowerState(d.avion_id);
  const cls = 'glow ' + (on ? 'lit' : !known(d) && !devicePowerIntents.has(d.avion_id) ? 'unknown' : '');
  let style = '';
  if (on) { const c = kelvinColor(d.has_color_temp && d.color_temp ? d.color_temp : 2700), p = known(d) ? pct(d.brightness) : 100;
    style = `style="background:${c};opacity:${(.35 + .65 * p / 100).toFixed(2)};box-shadow:0 0 ${6 + p / 8}px ${c}"`; }
  if (!button || !feat('control')) return `<span class="${cls}" ${style} title="${esc(devicePowerHint(d.avion_id))}"></span>`;
  return `<button class="${cls}" ${style} title="${esc(devicePowerHint(d.avion_id))}" aria-label="${on ? 'Turn off' : 'Turn on'} ${esc(d.name)}" onclick="event.stopPropagation();toggleLight(${d.avion_id})"></button>`;
}
function toast(msg) {
  const t = document.createElement('div'); t.className = 'toast'; t.setAttribute('role', 'status'); t.textContent = msg;
  $('#toastRoot').replaceChildren(t); clearTimeout(toast.t); toast.t = setTimeout(() => t.remove(), 4000);
}
function log(kind, title, detail = '', rawData = '') {
  S.log.unshift({t:timeNow(), kind, title, detail, raw:rawData});
  if (S.log.length > 200) S.log.length = 200;
  if (S.view === 'activity') renderSoon();
}

/* ---------- API ---------- */
const ERRORS = {
  operation_unavailable_or_uncertain: 'The bridge could not confirm the operation. Review Operations before trying another change.',
  invalid_request: 'The bridge refused that request.',
  timeout_outcome_unknown: 'No reply in time, so it may or may not have happened.',
  commissioning_capability_disabled: 'Pairing is switched off on this bridge.',
  invalid_origin: 'The bridge refused a request from this page. Reload it.',
  invalid_host: 'Open the bridge by its usual address and reload.',
};
async function api(path, data, opts = {}) {
  for (let attempt = 0; ; attempt++) {
    const r = await fetch(path, data === undefined ? {cache:'no-store'}
      : {method:'POST', cache:'no-store', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    const body = await r.json().catch(() => ({}));
    // Retry only an explicit busy refusal before dispatch. An uncertain result is never replayed.
    if (opts.retryBusy && r.status === 409 && body.retry_allowed === true && operationNotStarted(body) &&
        ['background_read_busy', 'mesh_busy'].includes(body.error) && attempt < 10) { await sleep(800); continue; }
    if (!r.ok) { const e = new Error(body.message || ERRORS[body.error] || body.error || 'The bridge returned HTTP ' + r.status); e.status = r.status; e.code = body.error; e.details = body; throw e; }
    return body;
  }
}
function operationNotStarted(data) {
  return data?.operation_started === false && data.requires_review === false &&
    ['background_read_busy', 'mesh_busy', 'mesh_offline', 'session_changed', 'inventory_changed',
     'anyplace_wake_timeout', 'anyplace_admission_expired'].includes(data.error);
}
const getJson = path => api(path);
function lightValues(values) {
  const v = {};
  if (values.state !== undefined) v.state = values.state;
  if (values.brightness !== undefined) { if (values.brightness > 0) { v.state = 'ON'; v.brightness = values.brightness; } else v.state = 'OFF'; }
  if (values.color_temp !== undefined) v.color_temp = values.color_temp;
  return v;
}
const sendControl = (id, values, confirm = true, retryBusy = true) => api('/api/control', {target:id, values:lightValues(values), confirm}, {retryBusy});

/* One Bluetooth job at a time. Each job is an async function. */
function radio(label, job, quiet = false, background = false) {
  return new Promise((resolve, reject) => {
    const waiting = !background ? S.radio.queue.findIndex(item => item.background) : -1;
    S.radio.queue.splice(waiting < 0 ? S.radio.queue.length : waiting, 0, {label, job, resolve, reject, quiet, background});
    if (!quiet && S.radio.label && S.radio.queue.length === 1) toast(`Bluetooth is busy (${S.radio.label}). "${label}" will run next.`);
    pumpRadio();
  });
}
async function pumpRadio() {
  if (S.radio.label || !S.radio.queue.length) return;
  const {label, job, resolve, reject, quiet} = S.radio.queue.shift();
  S.radio.label = label; S.radio.quiet = quiet; S.radio.step = ''; renderBusy();
  try { resolve(await job(step => { S.radio.step = step; renderBusy(); })); }
  catch (e) { reject(e); }
  finally { S.radio.label = null; renderBusy(); pumpRadio(); }
}

let activeFlash = null;
async function control(id, values, label, canDispatch = null) {
  const instance = S.status?.instance, session = S.status?.diagnostics?.session;
  if (activeFlash && (values.state || values.brightness !== undefined) &&
      (id === activeFlash.id || grp(id)?.members.includes(activeFlash.id))) activeFlash.interrupted = true;
  if (id !== 0) S.pending[id] = true;
  renderSoon();
  try {
    const res = await radio(label, () => {
      if (instance && (!S.synced || S.status?.instance !== instance || S.status?.diagnostics?.session !== session)) return null;
      return canDispatch && !canDispatch() ? null : sendControl(id, values, false);
    }, true);
    if (res === null) return null;
    const replies = Array.isArray(res.readbacks) ? res.readbacks.length : 0;
    const confirmed = res.confirmed === true && replies > 0;
    const detail = res.feedback_pending === true ? 'Command sent; light readings update separately.'
      : !replies ? 'Sent, but no light replied. The change is not confirmed.'
      : confirmed ? `Confirmed; ${replies} light${replies === 1 ? '' : 's'} replied.`
      : `Sent; ${replies} light${replies === 1 ? '' : 's'} replied, but the full change is not confirmed.`;
    if (!confirmed && res.feedback_pending !== true) toast(detail);
    log('light', label, detail);
    return res;
  } catch (e) { toast(e.message); log('system', label + ' failed', e.message); throw e; }
  finally { delete S.pending[id]; renderSoon(); }
}

/* ---------- actions ---------- */
function toggleLight(id) { return devicePower(id, !devicePowerState(id)); }
function setBrightness(id, p) {
  const group = grp(id), d = dev(id);
  const targets = group ? members(id) : d ? [d] : [];
  if (!targets.length || !targets.every(canDim)) return;
  const values = {brightness:raw(+p)}, label = `Set ${(group || d).name} to ${p}%`;
  return group ? groupLevelControl(id, values, label) : deviceLevelControl(id, values, label);
}
function setKelvin(id, k) { const name = (dev(id) || grp(id) || {}).name;
  control(id, {color_temp: +k}, `Set ${name} to ${k} K`).catch(() => {}); }
// Desired switch position is separate from observed light state. Hold it only
// while the request/readback is pending; never manufacture a light observation.
const devicePowerIntents = new Map();
function devicePowerState(id) { return devicePowerIntents.get(id)?.on ?? (dev(id) ? isOn(dev(id)) : false); }
function devicePowerHint(id) {
  const intent = devicePowerIntents.get(id), d = dev(id);
  return intent ? `Requested ${intent.on ? 'On' : 'Off'}; last reported ${!d || !known(d) ? 'unknown' : isOn(d) ? pct(d.brightness) + '%' : 'Off'}. Waiting for a new light report.` : '';
}
function clearDevicePowerIntent(id, intent) {
  if (devicePowerIntents.get(id) !== intent) return;
  clearTimeout(intent.timer); devicePowerIntents.delete(id); renderSoon();
}
function settleDevicePowerIntents() {
  for (const [id, intent] of devicePowerIntents) {
    const d = dev(id);
    if (intent.instance !== S.status?.instance || !d || d.kind === 'controller') { clearDevicePowerIntent(id, intent); continue; }
    if (intent.running) continue;
    const matched = d.power_fresh && isOn(d) === intent.on &&
      Number.isFinite(d.power_observed_ms) && d.power_observed_ms > Math.max(intent.before ?? -1, intent.after ?? -1);
    if (matched || Date.now() >= intent.until) clearDevicePowerIntent(id, intent);
  }
}
function deviceLevelControl(id, values, label) {
  const d = dev(id); if (!d || d.kind === 'controller') return;
  const prior = devicePowerIntents.get(id); if (prior) clearTimeout(prior.timer);
  const intent = {on:values.state === 'ON' || values.brightness > 0, running:true, instance:S.status?.instance, before:d.power_observed_ms};
  devicePowerIntents.set(id, intent); renderSoon();
  return control(id, values, label).then(async result => {
    if (devicePowerIntents.get(id) !== intent) return;
    if (result === null) { clearDevicePowerIntent(id, intent); return; }
    intent.after = Number.isFinite(result.feedback_after_ms) ? result.feedback_after_ms : -1;
    intent.running = false; intent.until = Date.now() + 8000;
    intent.timer = setTimeout(() => clearDevicePowerIntent(id, intent), 8000);
    await refreshSnapshot(); settleDevicePowerIntents(); renderSoon();
  }, () => clearDevicePowerIntent(id, intent));
}
function devicePower(id, on) { return deviceLevelControl(id, {state:on ? 'ON' : 'OFF'}, `Turn ${dev(id)?.name || 'light'} ${on ? 'on' : 'off'}`); }
const groupPowerIntents = new Map();
function groupPowerState(gid) { return groupPowerIntents.get(gid)?.on ?? members(gid).some(isOn); }
function clearGroupPowerIntent(gid, intent) {
  if (groupPowerIntents.get(gid) !== intent) return;
  clearTimeout(intent.timer); groupPowerIntents.delete(gid); renderSoon();
}
function settleGroupPowerIntents() {
  for (const [gid, intent] of groupPowerIntents) {
    if (intent.instance !== S.status?.instance || !grp(gid)) { clearGroupPowerIntent(gid, intent); continue; }
    if (intent.running) continue;
    const ms = members(gid);
    const matched = ms.length && ms.every(d => d.power_fresh && isOn(d) === intent.on &&
      Number.isFinite(d.power_observed_ms) && d.power_observed_ms > Math.max(intent.before[d.avion_id] ?? -1, intent.after ?? -1));
    if (matched || Date.now() >= intent.until) clearGroupPowerIntent(gid, intent);
  }
}
function groupLevelControl(gid, values, label) {
  const prior = groupPowerIntents.get(gid); if (prior) clearTimeout(prior.timer);
  const intent = {on:values.state === 'ON' || values.brightness > 0, running:true, instance:S.status?.instance,
    before:Object.fromEntries(members(gid).map(d => [d.avion_id,d.power_observed_ms]))};
  groupPowerIntents.set(gid, intent); renderSoon();
  return control(gid, values, label).then(async result => {
    if (groupPowerIntents.get(gid) !== intent) return;
    if (result === null) { clearGroupPowerIntent(gid, intent); return; }
    intent.after = Number.isFinite(result.feedback_after_ms) ? result.feedback_after_ms : -1;
    intent.running = false; intent.until = Date.now() + 8000;
    intent.timer = setTimeout(() => clearGroupPowerIntent(gid, intent), 8000);
    await refreshSnapshot(); settleGroupPowerIntents(); renderSoon();
  }, () => clearGroupPowerIntent(gid, intent));
}
function groupPower(gid, on) { return groupLevelControl(gid, {state:on ? 'ON' : 'OFF'}, `Turn ${gName(gid)} ${on ? 'on' : 'off'}`); }
function groupPowerSwitch(gid, label) {
  return `<span class="group-power"><label class="switch"><input type="checkbox" ${groupPowerState(gid) ? 'checked' : ''} aria-label="${esc(label)}" onchange="groupPower(${gid},this.checked)"><span></span></label>${members(gid).some(d => d.power_last_known) ? '<span class="small muted">Last known</span>' : ''}</span>`;
}

/* A room isn't a mesh group. Use a group with exactly the room's lights when one exists; otherwise send to each light. */
function roomGroup(room) {
  const ids = lights().filter(d => roomOf(d) === room).map(d => d.avion_id).sort().join();
  return S.groups.find(g => g.members.length && [...g.members].sort().join() === ids);
}
// Rooms without an exact mesh group still need stable requested switch feedback.
// Keep it separate from observations, including while their lights run in sequence.
const roomPowerIntents = new Map();
function roomLights(room) { return lights().filter(d => roomOf(d) === room); }
function roomPowerState(room) { return roomPowerIntents.get(room)?.on ?? roomLights(room).some(isOn); }
function clearRoomPowerIntent(room, intent) {
  if (roomPowerIntents.get(room) !== intent) return;
  clearTimeout(intent.timer); roomPowerIntents.delete(room); renderSoon();
}
function roomPowerIntentCurrent(room, intent) {
  const ls = roomLights(room);
  return S.synced && roomPowerIntents.get(room) === intent && intent.instance === S.status?.instance &&
    !roomGroup(room) && ls.length === intent.ids.length && ls.every(d => intent.ids.includes(d.avion_id));
}
function settleRoomPowerIntents() {
  for (const [room, intent] of roomPowerIntents) {
    const ls = roomLights(room);
    if (!roomPowerIntentCurrent(room, intent)) {
      clearRoomPowerIntent(room, intent); continue;
    }
    if (intent.running) continue;
    const matched = ls.length && ls.every(d => d.power_fresh && isOn(d) === intent.on &&
      Number.isFinite(d.power_observed_ms) && d.power_observed_ms >
        Math.max(intent.before[d.avion_id] ?? -1, intent.after[d.avion_id] ?? -1));
    if (matched || Date.now() >= intent.until) clearRoomPowerIntent(room, intent);
  }
}
async function roomLevelControl(room, values) {
  const ls = roomLights(room); if (!ls.length) return;
  const prior = roomPowerIntents.get(room); if (prior) clearTimeout(prior.timer);
  const intent = {on:values.state === 'ON' || values.brightness > 0, running:true, instance:S.status?.instance,
    ids:ls.map(d => d.avion_id), before:Object.fromEntries(ls.map(d => [d.avion_id,d.power_observed_ms])), after:{}};
  roomPowerIntents.set(room, intent); renderSoon();
  try {
    for (const d of ls) {
      // A newer room request supersedes unsent work from this one.
      if (roomPowerIntents.get(room) !== intent) return;
      const label = values.state ? `Turn ${d.name} ${values.state.toLowerCase()}` : `Set ${d.name} to ${pct(values.brightness)}%`;
      const result = await control(d.avion_id, values, label,
        () => roomPowerIntentCurrent(room, intent));
      if (result === null) { clearRoomPowerIntent(room, intent); return; }
      intent.after[d.avion_id] = Number.isFinite(result.feedback_after_ms) ? result.feedback_after_ms : -1;
    }
    if (roomPowerIntents.get(room) !== intent) return;
    intent.running = false; intent.until = Date.now() + 8000;
    intent.timer = setTimeout(() => clearRoomPowerIntent(room, intent), 8000);
    await refreshSnapshot(); settleRoomPowerIntents(); renderSoon();
  } catch (e) { clearRoomPowerIntent(room, intent); }
}
function roomPower(room, on) {
  const g = roomGroup(room);
  return g ? groupPower(g.group_id, on) : roomLevelControl(room, {state:on ? 'ON' : 'OFF'});
}
function roomBrightness(room, p) {
  const g = roomGroup(room);
  if (!roomLights(room).every(canDim)) return;
  return g ? setBrightness(g.group_id, p) : roomLevelControl(room, {brightness:raw(+p)});
}
let stateSweep = null;
function stopStateSweep() {
  if (stateSweep) stateSweep.stopped = true;
  renderSoon();
}
async function readStates() {
  if (stateSweep) return;
  const targets = lights().map(d => d.avion_id);
  if (!targets.length) return;
  const sweep = stateSweep = {stopped:false, instance:S.status?.instance};
  renderSoon();
  let checked = 0, replies = 0;
  try {
    for (const id of targets) {
      if (sweep.stopped || !S.synced || S.status?.instance !== sweep.instance) break;
      await radio('Reading light states', async step => {
        const d = dev(id);
        if (sweep.stopped || !S.synced || S.status?.instance !== sweep.instance || d?.kind !== 'light') return;
        step(`${checked + 1} of ${targets.length}: ${d.name}`);
        const r = await api('/api/device/read', {target:id});
        checked++;
        if (Object.keys(r.observed || {}).length) replies++;
      }, false, true);
    }
    const message = `${replies} of ${checked} checked lights replied.`;
    const result = checked < targets.length ? 'Read stopped. ' + message : message;
    toast(result); log('system', 'Read light states', result);
  } catch (e) {
    const message = `Read stopped after ${checked} of ${targets.length} lights. ${e.message}`;
    toast(message); log('system', 'Read light states', message);
  } finally { stateSweep = null; renderSoon(); }
}
/* Read the actual level once the flash reaches the radio queue. A stale display
   or a command queued ahead of this request cannot supply its restoration level. */
async function flash(id) {
  const device = dev(id); if (device?.kind !== 'light') return;
  const instance = S.status?.instance, session = S.status?.diagnostics?.session;
  const stillCurrent = () => S.synced && dev(id)?.product_type === device.product_type && S.status?.instance === instance &&
    S.status?.diagnostics?.session === session && S.status?.mesh_ready === true;
  try {
    await radio(`Flashing ${device.name}`, async step => {
      const run = activeFlash = {id, interrupted:false};
      try {
        if (!stillCurrent()) throw new Error('The light connection changed. Open its details again.');
        step('Reading the starting level');
        const read = await api('/api/device/read', {target:id}, {retryBusy:true});
        if (!stillCurrent() || read.target !== id) throw new Error('The starting reading could not be matched to this light.');
        const level = read.observed?.brightness;
        let before = Number.isInteger(level) && level >= 0 && level <= 255 ? level : null;
        if (before === null && read.observed?.state === 'OFF') before = 0;
        if (before === null && !canDim(device) && read.observed?.state === 'ON') before = 255;
        if (run.interrupted) return;
        const unknown = before === null;
        if (unknown) {
          const end = canDim(device) ? 'on at full brightness' : 'on';
          if (!confirm(`${device.name} did not report its starting level. Flash twice and leave it ${end}?`)) return;
          before = 255;
        }
        const other = before > 0 ? 0 : 255;
        toast(`${device.name} will blink twice. Look for it.`);
        for (const [i, brightness] of [other, before, other, before].entries()) {
          if (run.interrupted) { log('light', 'Flash stopped', 'A newer power or brightness request is waiting.'); return; }
          if (!stillCurrent()) throw new Error('The light connection changed during the flash. Check its current state.');
          step(`Blink ${Math.floor(i / 2) + 1} of 2`);
          const values = canDim(device) ? {brightness} : {state:brightness > 0 ? 'ON' : 'OFF'};
          const result = await sendControl(id, values, true, false);
          if (!result.confirmed) throw new Error('The light did not confirm its level during the flash. Check its current state.');
          if (i < 3) await sleep(250);
        }
        log('light', `Flashed ${device.name}`, unknown ? 'Finished on as requested' : 'Restored its starting level');
      } finally { activeFlash = null; }
    });
  } catch (e) { toast(e.message); }
}
async function saveLabels(entries, quiet) {
  const rooms = {}, names = [];
  Object.entries(entries).forEach(([id, e]) => { if (e.room !== undefined) rooms[id] = e.room; if (e.name !== undefined) names.push([+id, e.name]); });
  try {
    if (Object.keys(rooms).length) {
      const r = await api('/api/device-rooms', {rooms});
      S.roomMap = r.device_rooms || S.roomMap;
      S.devices.forEach(d => d.room = S.roomMap[String(d.avion_id)] || '');
    }
    for (const [id, name] of names) {
      const d = dev(id); if (!d) continue;
      try {
        const r = await api('/api/metadata', {operation:'update', revision:S.revision, kind:'device', target:id, name, exposed:d.exposed === true});
        S.revision = r.revision; d.name = name;
      } catch (e) {
        if (e.status === 400) throw new Error('The device list changed while saving, so that name wasn\'t saved. Try again.');
        throw e;
      }
    }
    renderSoon();
    if (!quiet) toast('Saved');
    return true;
  } catch (e) { toast(e.message); refreshSnapshot(); return false; }
}
const deviceNameDrafts = new Map(), deviceRoomDrafts = new Map();
function editDeviceName(id, value) { deviceNameDrafts.set(id, {value, error:'', saving:false}); }
function validLabel(value, limit) { return value.length > 0 && [...value].length <= limit && !/[\p{Cc}\p{Cf}\p{Cs}]/u.test(value); }
async function renameDevice(id, value) {
  const d = dev(id); if (!d || deviceNameDrafts.get(id)?.saving) return;
  const v = value.trim(), draft = {value, error:'', saving:false}; deviceNameDrafts.set(id, draft);
  if (!validLabel(v, 80)) { draft.error = 'Enter a name with 1–80 printable characters.'; renderSoon(); return; }
  if (v === d.name) { deviceNameDrafts.delete(id); renderSoon(); return; }
  const previous = d.name; draft.saving = true;
  if (S.drawer?.id === id && $('#dName')) $('#dName').disabled = true;
  renderSoon();
  const ok = await saveLabels({[id]:{name:v}});
  if (deviceNameDrafts.get(id) === draft) {
    if (ok) { deviceNameDrafts.delete(id); log('system', `Renamed "${previous}" to "${v}"`); }
    else Object.assign(draft, {saving:false, error:'Name was not confirmed. Your text is kept; review it before saving again.'});
  }
  renderSoon();
}
function editNewRoom(id, value) { const d = deviceRoomDrafts.get(id); if (d && !d.saving) { d.name = value; d.error = ''; } }
function cancelNewRoom(id) { if (!deviceRoomDrafts.get(id)?.saving) { deviceRoomDrafts.delete(id); render(); } }
async function setRoom(id, value) {
  if (deviceRoomDrafts.get(id)?.saving) return;
  if (value === '__new') { deviceRoomDrafts.set(id, {editing:true,name:'',error:'',saving:false}); renderSoon(); return; }
  const draft = {editing:false, selection:value, saving:true, error:''}; deviceRoomDrafts.set(id,draft); renderSoon();
  const ok = await saveLabels({[id]:{room:value}},true);
  if (deviceRoomDrafts.get(id) === draft) {
    if (ok) { deviceRoomDrafts.delete(id); toast(value ? 'Moved to '+value : 'Removed from room'); }
    else Object.assign(draft,{saving:false,selection:roomOf(dev(id)),error:'Room was not confirmed. Review the selection before trying again.'});
  }
  renderSoon();
}
async function saveNewRoom(id) {
  const draft = deviceRoomDrafts.get(id); if (!draft?.editing || draft.saving) return;
  const value = ($('#newRoomName')?.value || draft.name || '').trim().replace(/\s+/g,' ');
  if (!validLabel(value,40)) { draft.error = 'Enter a room name with 1–40 printable characters.'; render(); return; }
  draft.saving = true; render();
  const ok = await saveLabels({[id]:{room:value}},true);
  if (deviceRoomDrafts.get(id) === draft) {
    if (ok) { deviceRoomDrafts.delete(id); toast('Moved to '+value); }
    else Object.assign(draft,{saving:false,error:'Room was not confirmed. Your text is kept; review it before trying again.'});
  }
  renderSoon();
}
let drawerReturnFocus = null;
function go(view, receipt = null) { if (typeof Ops !== 'undefined') Ops.selected = view === 'operations' ? receipt : null; if (view === 'scenes') refreshScenes(); activeNativeSelect = null; S.view = view; S.drawer = null; try { localStorage.setItem('halo-view', view); } catch (e) {} render(); window.scrollTo(0, 0); }
function openDrawer(kind, id) { drawerReturnFocus = document.activeElement; activeNativeSelect = null; S.drawer = {kind, id}; render(); focusDialog(); }
function closeDrawer(force) {
  if (force !== true && S.drawer?.kind === 'scene' && sceneDraft?.dirty && !sceneRunning()) {
    modal(`<h3>Discard changes to this scene?</h3><p>Your changes have not been saved. Nothing has been sent to the lights.</p><div class="row"><button class="btn" onclick="closeModal()">Keep editing</button><button class="btn danger" onclick="closeModal();closeDrawer(true)">Discard changes</button></div>`);
    return;
  }
  activeNativeSelect = null; S.drawer = null; render(); restoreDialogFocus(drawerReturnFocus); drawerReturnFocus = null;
}

/* ---------- Home Assistant sharing ---------- */
// Sharing is bridge metadata only. Never retry a save or configure controller buttons here.
let haShareBusy = false;
const haShareResults = new Map();
function haShareItem(kind, id) { return kind === 'device' ? dev(id) : kind === 'group' ? grp(id) : null; }
function haShareSupported(kind, item) {
  if (!item || item.protected || kind === 'group' && item.group_id === 24566) return false;
  return kind === 'group' || Object.hasOwn(S.caps.types?.lights || {}, item.product_type) ||
    Object.hasOwn(S.caps.types?.controllers || {}, item.product_type);
}
function haShareAllowed(kind, id) {
  const item = haShareItem(kind, id);
  return feat('group_metadata') && managementFresh() && S.revision && !haShareBusy &&
    !haShareResults.get(kind + ':' + id)?.review && haShareSupported(kind, item) &&
    (item.exposed === true || S.status?.mqtt_enabled === true);
}
function haSharingHtml(kind, item) {
  const id = kind === 'device' ? item.avion_id : item.group_id;
  if (!haShareSupported(kind, item) || !feat('group_metadata')) return '';
  const shared = item.exposed === true, enabled = S.status?.mqtt_enabled === true, connected = S.status?.mqtt_ready === true;
  const result = haShareResults.get(kind + ':' + id);
  const status = shared ? 'Selected for Home Assistant.' : 'Available only on this bridge.';
  const delivery = !enabled ? 'Configure MQTT in Settings to connect Home Assistant.' : !connected ?
    'The MQTT broker is disconnected. Discovery changes wait for a connection.' :
    'MQTT is connected. Check Home Assistant to confirm discovery.';
  return `<div class="sec"><h4>Home Assistant</h4><p>${status} ${delivery}</p>
    <button class="btn" onclick="setHaSharing('${kind}',${id},${!shared})" ${haShareAllowed(kind, id) ? '' : 'disabled'}>${haShareBusy ? 'Saving…' : shared ? 'Remove from Home Assistant' : 'Add to Home Assistant'}</button>
    ${!enabled ? '<button class="btn" onclick="openMqttSettings()">Configure MQTT</button>' : ''}
    ${kind === 'device' && item.kind === 'controller' ? '<p class="hint">Shares this controller’s events with Home Assistant. It does not change button assignments or setup.</p>' : ''}
    ${!managementFresh() ? '<p class="hint">Refresh the device list before changing sharing.</p>' : ''}
    ${result ? `<p role="status">${esc(result.message)}</p>` : ''}
    ${result?.review || !managementFresh() ? `<button class="btn" onclick="refreshHaSharing('${kind}',${id})" ${haShareBusy ? 'disabled' : ''}>Refresh sharing status</button>` : ''}</div>`;
}
async function refreshHaSharing(kind, id) {
  if (haShareBusy) return;
  try { await refreshSnapshot(true); haShareResults.delete(kind + ':' + id); }
  catch (e) { toast('Could not refresh sharing status. Try again when the bridge is reachable.'); }
  renderSoon();
}
async function setHaSharing(kind, id, exposed) {
  if (!haShareAllowed(kind, id) || typeof exposed !== 'boolean') return;
  const item = haShareItem(kind, id), key = kind + ':' + id;
  if (item.exposed === exposed) return;
  haShareBusy = true;
  haShareResults.set(key, {message:'Saving Home Assistant sharing…'}); renderSoon();
  try {
    const result = await api('/api/metadata', {operation:'update', revision:S.revision, kind, target:id, name:item.name, exposed});
    if (result.saved !== true || !result.revision) throw new Error('Save was not confirmed.');
    S.revision = result.revision; item.exposed = exposed;
    haShareResults.set(key, {message:exposed ? 'Sharing saved. Check Home Assistant for the new entity.' :
      'Sharing removed. Home Assistant updates when MQTT is connected.'});
    // Read back metadata, including any concurrent edits; a failed refresh never repeats the save.
    await refreshSnapshot(true);
  } catch (e) {
    const refused = [400,401,403,409,501].includes(e.status);
    haShareResults.set(key, {review:true, message:refused ?
      'Sharing was not confirmed. Refresh the device list before trying again. ' + e.message :
      'The sharing response was not confirmed. Refresh sharing status before another change. Nothing is retried automatically.'});
  } finally { haShareBusy = false; renderSoon(); }
}

/* ---------- attention ---------- */
function duplicateGroups() {
  const out = []; const gs = lightingGroups().filter(g => g.members.length);
  for (let i = 0; i < gs.length; i++) for (let j = i + 1; j < gs.length; j++)
    if ([...gs[i].members].sort().join() === [...gs[j].members].sort().join()) out.push([gs[i], gs[j]]);
  return out;
}
function attentionItems() {
  const items = [], ls = lights();
  if (!S.synced) return items;
  const st = S.status || {};
  if (st.pending_transactions) items.push({t:`${st.pending_transactions} unfinished operation${st.pending_transactions === 1 ? ' needs' : 's need'} review`, s:'Review them in Operations. Nothing is retried automatically.', a:'Review operations', f:"go('operations')"});
  if (st.publication_pending) items.push({t:`${st.publication_pending} removed device${st.publication_pending === 1 ? ' needs' : 's need'} Home Assistant cleanup`, s:'Removal is confirmed. Review the remaining publication cleanup in Operations.', a:'Review cleanup', f:"go('operations')"});
  if (st.last_error && st.last_error !== 'bluetooth_connection_failed') items.push({t:'The bridge reported a problem', s:String(st.last_error), a:'Settings', f:"go('settings')"});
  if (st.mesh_ready === false && st.bluetooth_startup_retrying) items.push({t:"Bluetooth hasn't connected yet", s:'The bridge is retrying. Check that a nearby HALO light has power and Bluetooth is enabled on the Pi.', a:'Settings', f:"go('settings')"});
  else if (st.mesh_ready === false && !st.bluetooth_starting) items.push({t:'Bluetooth is disconnected', s:'Check that a nearby HALO light has power and Bluetooth is enabled on the Pi.', a:'Settings', f:"go('settings')"});
  const dl = dupNames(ls);
  if (dl.length) items.push({t:`${dl.length} lights share a name with another light`, s:'Flash each one to see which is which, then give it a name.', a:'Name lights', f:'openNamer()'});
  const dc = dupNames(controllers());
  if (dc.length) items.push({t:`${dc.length} controllers share a name`, s:'Rename them so you can tell them apart.', a:'Show them', f:`S.controllerQ=${esc(JSON.stringify(dc[0].name))};go('controllers')`});
  if (feat('labels') && ls.length && !ls.some(roomOf)) items.push({t:'Lights aren\'t sorted into rooms yet', s:'Rooms organise this page. You can start from your groups.', a:'Sort into rooms', f:'openRoomSetup()'});
  duplicateGroups().forEach(([a, b]) => items.push({t:`"${a.name}" and "${b.name}" control the same ${a.members.length} lights`, s:'One of them is probably left over from an earlier setup.', a:'View', f:`openDrawer('group',${a.group_id})`}));
  lightingGroups().filter(g => !g.members.length).forEach(g => items.push({t:`"${g.name}" has no lights`, s:feat('group_edit') ? 'Add lights to it or delete it.' : 'Group editing isn\'t available on this bridge yet.', a:'View', f:`openDrawer('group',${g.group_id})`}));
  const unk = ls.filter(d => missingPowerReply(d)).length, sr = st.startup_read;
  if (unk && st.mesh_ready !== false && ls.every(d => d.power_read_status === undefined) && sr && (sr.state === 'waiting' || sr.state === 'running'))
    items.push({t:'Checking lights after a restart', s:sr.state === 'running' ? `${sr.answered + sr.skipped} of ${sr.lights} checked so far.` : 'Starts when the bridge reaches the mesh.', a:'Devices', f:"go('devices')"});
  else if (unk && st.mesh_ready !== false && feat('read_states')) items.push({t:`${unk} light${unk === 1 ? ' hasn\'t' : 's haven\'t'} reported a state`, s:ls.some(d => d.power_read_status === 'no_reply') ? 'Three power checks received no reply from each affected light.' : 'Open Devices to see which readings are missing.', a:'View lights', f:"S.filter='att';S.q='';go('devices')"});
  return items;
}

/* ---------- views ---------- */
function viewHome() {
  const ls = lights(), cs = controllers(), gs = lightingGroups();
  const att = attentionItems(), rs = rooms(), waiting = ls.filter(gettingStatus).length;
  const roomDevices = [...ls, ...cs], assignedRooms = [...new Set(roomDevices.map(roomOf).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const unroomedLights = ls.filter(d => !roomOf(d));
  return `<div class="mobile-health">${healthLine()}</div>
  <div class="view-head"><div><h2>Home</h2><div class="sub">${powerCounts(ls)} · ${cs.length} controller${cs.length === 1 ? '' : 's'} · ${rs.length} room${rs.length === 1 ? '' : 's'}</div></div>
    <div class="actions">${feat('read_states') ? (stateSweep ? `<button class="btn" onclick="stopStateSweep()" ${stateSweep.stopped ? 'disabled' : ''}>${stateSweep.stopped ? 'Stopping read…' : 'Stop reading'}</button>` : '<button class="btn" onclick="readStates()">Read light states</button>') : ''}${feat('pair_lights') ? `<button class="btn primary" onclick="go('add')">${I.plus}Add devices</button>` : ''}</div></div>
  ${S.status?.bluetooth_starting && !S.status.bluetooth_startup_retrying ? `<div class="callout info" role="status">${I.info}<div>Connecting to the lights over Bluetooth…</div></div>` : ''}
  ${waiting ? `<div class="callout info" role="status">${I.info}<div><b>Getting status for ${waiting} light${waiting === 1 ? '' : 's'}…</b><p>Each light updates when it reports its state.</p></div></div>` : ''}
  ${att.length ? `<section class="panel attention" aria-labelledby="attH"><h3 id="attH"><span class="badge warn">${att.length}</span>Needs attention</h3><ul>
    ${att.map(a => `<li><div class="t"><b>${esc(a.t)}</b><span>${esc(a.s)}</span></div><button class="btn sm" onclick="${a.f}">${a.a}</button></li>`).join('')}</ul></section>` : ''}
  <section class="home-section" aria-labelledby="homeGroups"><div class="section-head"><h3 id="homeGroups">Groups <span class="badge">${gs.length}</span></h3><button class="link" onclick="go('groups')">Manage groups</button></div>
    <p class="hint">For the best response when controlling several lights together, use a group.</p>
    ${gs.length ? `<div class="overview-grid">${gs.map(homeGroupCard).join('')}</div>` : '<p class="section-empty">No lighting groups yet. Create one from Groups.</p>'}</section>
  <section class="home-section" aria-labelledby="homeLights"><div class="section-head"><h3 id="homeLights">Lights <span class="badge">${ls.length}</span></h3><button class="link" onclick="S.filter='lights';S.q='';go('devices')">View lights</button></div>
    <p class="hint">Rooms organise this page. Adding a light to a room does not add it to a group. Create and manage lighting groups in Groups.</p>
    ${cs.length ? '<p class="hint">Room cards also show their controllers.</p>' : ''}
    ${assignedRooms.length || unroomedLights.length ? `<div class="rooms">${assignedRooms.map(r => roomCard(r, ls.filter(d => roomOf(d) === r), cs.filter(d => roomOf(d) === r))).join('')}${unroomedLights.length ? roomCard('', unroomedLights) : ''}</div>` : '<p class="section-empty">No lights paired yet.</p>'}</section>
  <section class="home-section" aria-labelledby="homeControllers"><div class="section-head"><h3 id="homeControllers">Controllers <span class="badge">${cs.length}</span></h3><button class="link" onclick="S.controllerQ='';go('controllers')">View controllers</button></div>
    ${cs.length ? `<div class="tiles controller-tiles">${cs.slice().sort(byName).map(d => ctrlTile(d)).join('')}</div>` : '<p class="section-empty">No controllers paired yet. Add a keypad or dimmer to get started.</p>'}</section>
`;
}
function roomCard(room, ls, cs = []) {
  const on = ls.filter(isOn), title = room || (rooms().length || cs.length ? NO_ROOM : 'All lights');
  const counts = [ls.length ? powerCounts(ls) : '', cs.length ? `${cs.length} controller${cs.length === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · ');
  const avg = on.length ? Math.round(on.reduce((s, d) => s + pct(d.brightness), 0) / on.length) : 0;
  const rid = 'room-' + btoa(unescape(encodeURIComponent(title))).replace(/[^a-z0-9]/gi, '');
  const canControl = feat('control') && room && ls.length, group = room && roomGroup(room);
  return `<section class="room" aria-labelledby="${rid}">
    <div class="room-head"><h4 id="${rid}">${esc(title)}</h4><span class="count">${counts}</span>
      ${canControl ? `<div class="rc">${ls.every(canDim) ? `<input type="range" min="0" max="100" value="${avg}" aria-label="${esc(title)} brightness" onchange="roomBrightness(${esc(JSON.stringify(room))},this.value)">` : ''}
      ${group ? groupPowerSwitch(group.group_id, title + ' power') : `<label class="switch"><input type="checkbox" ${roomPowerState(room) ? 'checked' : ''} aria-label="${esc(title)} power" onchange="roomPower(${esc(JSON.stringify(room))},this.checked)"><span></span></label>`}</div>` : ''}</div>
    ${canControl && ls.length > 1 ? `<p class="hint room-control-hint">${group ? `Room controls use the ${esc(group.name)} group.` : 'Room controls send to each light separately. Create a group with these lights for a faster, more coordinated response.'}</p>` : ''}
    <div class="tiles">${ls.slice().sort(byName).map(tile).join('')}${cs.slice().sort(byName).map(d => ctrlTile(d, !!room)).join('')}</div>
    ${room && ls.length && feat('group_metadata') ? `<div class="row" style="margin-top:12px"><button class="btn sm" onclick="${group ? `openDrawer('group',${group.group_id})` : `openCreateGroup(${esc(JSON.stringify(room))})`}">${group ? 'Open group' : 'Create group from room'}</button></div>` : ''}
    ${!room && rooms().length === 0 && feat('labels') && ls.length ? `<p class="hint">Sort lights into rooms to make this page easier to scan. <button class="link" onclick="openRoomSetup()">Sort into rooms</button></p>` : ''}
  </section>`;
}
function homeGroupCard(g) {
  const ms = members(g.group_id);
  const state = ms.length ? powerCounts(ms) : 'No lights added';
  return `<div class="group-overview"><button class="group-open" onclick="openDrawer('group',${g.group_id})">${I.layers}<span><b>${esc(g.name)}</b><span class="small muted">${state}</span></span></button>
    ${ms.length && feat('control') ? groupPowerSwitch(g.group_id, g.name + ' group power') : ''}</div>`;
}
function tile(d) {
  return `<div class="tile ${devicePowerState(d.avion_id) ? 'on' : ''}" role="button" tabindex="0" onclick="openDrawer('device',${d.avion_id})" onkeydown="if(event.key==='Enter')openDrawer('device',${d.avion_id})">${glow(d)}
    <div class="tx"><div class="nm">${esc(d.name)}</div><div class="st ${S.pending[d.avion_id] ? 'pending' : ''}">${stateText(d)}</div></div></div>`;
}
function ctrlTile(d, inRoomCard = false) {
  return `<div class="tile ctrl" role="button" tabindex="0" onclick="openDrawer('device',${d.avion_id})" onkeydown="if(event.key==='Enter')openDrawer('device',${d.avion_id})">
    <span class="glow" style="display:grid;place-items:center;border:0;background:none;color:var(--muted)">${[126,127].includes(d.product_type) ? I.keypad : I.dimmer}</span>
    <div class="tx"><div class="nm">${esc(d.name)}</div><div class="st">${esc(typeName(d))}</div>${roomOf(d) && !inRoomCard ? `<div class="st">${esc(roomOf(d))}</div>` : ''}</div></div>`;
}

function viewDevices(controllerOnly = false) {
  const att = new Set([...dupNames(lights()), ...dupNames(controllers()), ...lights().filter(missingPowerReply)].map(d => d.avion_id));
  let rows = controllerOnly ? controllers() : S.devices.slice();
  const query = controllerOnly ? S.controllerQ : S.q;
  if (!controllerOnly && S.filter === 'lights') rows = rows.filter(d => d.kind !== 'controller');
  if (!controllerOnly && S.filter === 'ctrl') rows = rows.filter(d => d.kind === 'controller');
  if (!controllerOnly && S.filter === 'att') rows = rows.filter(d => att.has(d.avion_id));
  const q = query.trim().toLowerCase();
  if (q) rows = rows.filter(d => [d.name, roomOf(d), d.avion_id, typeName(d)].join(' ').toLowerCase().includes(q));
  rows.sort((a, b) => (roomOf(a) || '~').localeCompare(roomOf(b) || '~') || a.name.localeCompare(b.name, undefined, {numeric:true}));
  const f = (id, label, n) => `<button aria-pressed="${S.filter === id}" onclick="S.filter='${id}';render()">${label} <span class="muted">${n}</span></button>`;
  return `<div class="view-head"><div><h2>${controllerOnly ? 'Controllers' : 'Devices'}</h2><div class="sub">${controllerOnly ? 'Keypads and dimmers paired with this bridge' : "Everything in this bridge's inventory"}</div></div>
    ${feat('pair_lights') ? `<div class="actions"><button class="btn primary" onclick="go('add')">${I.plus}Add devices</button></div>` : ''}</div>
  <div class="toolbar"><label class="search">${I.search}<input id="devSearch" type="search" aria-label="${controllerOnly ? 'Search controllers' : 'Search devices'}" placeholder="Search by name, room, type or ID" value="${esc(query)}" oninput="S.${controllerOnly ? 'controllerQ' : 'q'}=this.value;renderMain(true)"></label>
    ${controllerOnly ? '' : `<div class="fseg" role="group" aria-label="Filter">${f('all', 'All', S.devices.length)}${f('lights', 'Lights', lights().length)}${f('ctrl', 'Controllers', controllers().length)}${f('att', 'Needs attention', att.size)}</div>`}</div>
  <div class="tablewrap"><table><thead><tr><th>Name</th><th>Room</th><th class="hide-sm">Type</th><th>State</th><th class="hide-sm">Mesh ID</th></tr></thead><tbody>
  ${rows.map(d => `<tr onclick="openDrawer('device',${d.avion_id})"><td><div class="nmcell">${d.kind === 'controller' ? `<span style="width:16px;height:16px;color:var(--muted)">${[126,127].includes(d.product_type) ? I.keypad : I.dimmer}</span>` : glow(d, false)}<button type="button" class="table-name" id="device-name-${d.avion_id}" onclick="event.stopPropagation();openDrawer('device',${d.avion_id})">${esc(d.name)}</button>${att.has(d.avion_id) ? '<span class="warnmark" title="Needs attention">!</span>' : ''}</div></td>
    <td>${esc(roomOf(d) || '–')}</td><td class="hide-sm">${esc(typeName(d))}</td><td>${d.kind === 'controller' ? '<span class="muted">Sends events</span>' : stateText(d)}</td><td class="hide-sm mono">#${d.avion_id}</td></tr>`).join('')
  || `<tr><td colspan="5" class="muted">${query ? `No ${controllerOnly ? 'controllers' : 'devices'} match "${esc(query)}".` : controllerOnly ? 'No controllers paired yet.' : 'No devices.'}</td></tr>`}
  </tbody></table></div>`;
}

function viewGroups() {
  const dup = new Set(duplicateGroups().flat().map(g => g.group_id));
  return `<div class="view-head"><div><h2>Groups</h2><div class="sub">Groups control several lights together for the best response. Room assignments do not create groups. Programmed groups are stored in the lights and work with wall controls while the bridge is off.</div></div>${feat('group_metadata') ? `<button class="btn primary" onclick="openCreateGroup()">${I.plus}Create group</button>` : ''}</div>
  ${feat('group_edit') ? '' : `<div class="callout info" style="margin-bottom:12px">${I.info}<div>You can create group names here. Adding lights requires membership programming and readback to be enabled on this bridge after physical testing.</div></div>`}
  <div class="tablewrap"><table><thead><tr><th>Group</th><th>Lights</th><th class="hide-sm">Rooms</th><th>Power</th></tr></thead><tbody>
  ${lightingGroups().map(g => { const ms = members(g.group_id); const rs = [...new Set(ms.map(roomOf).filter(Boolean))];
    return `<tr onclick="openDrawer('group',${g.group_id})"><td><button type="button" class="table-name" id="group-name-${g.group_id}" onclick="event.stopPropagation();openDrawer('group',${g.group_id})">${esc(g.name)}</button>${dup.has(g.group_id) ? ' <span class="badge warn">Same lights as another group</span>' : ''}${!ms.length ? ' <span class="badge warn">Empty</span>' : ''}</td>
    <td>${ms.length}</td><td class="hide-sm small">${esc(rs.join(', ') || '–')}</td>
    <td onclick="event.stopPropagation()">${ms.length && feat('control') && !g.protected ? groupPowerSwitch(g.group_id, g.name + ' power') : ''}</td></tr>`; }).join('')
  || '<tr><td colspan="4" class="muted">No lighting groups yet.</td></tr>'}
  </tbody></table></div>
  ${feat('all_lights') ? '<p class="hint">"All lights" is in Settings, behind a confirmation.</p>' : ''}`;
}

/* ---------- drawers ---------- */
function drawerHtml() {
  if (!S.drawer) return '';
  const {kind, id} = S.drawer; let h = '';
  if (kind === 'device') { const d = dev(id); if (!d) { S.drawer = null; return ''; } h = d.kind === 'controller' ? ctrlDrawer(d) : lightDrawer(d); }
  if (kind === 'group') { const g = grp(id); if (!g) { S.drawer = null; return ''; } h = groupDrawer(g); }
  if (kind === 'scene') h = sceneDrawer();
  return `<div class="scrim" onclick="closeDrawer()"></div><aside class="drawer" role="dialog" aria-modal="true" aria-label="Details" tabindex="-1">${h}</aside>`;
}
function roomField(d) {
  if (!feat('labels')) return `<dl class="kv"><dt>Room</dt><dd>${esc(roomOf(d) || '–')}</dd></dl>`;
  const draft = deviceRoomDrafts.get(d.avion_id), selected = draft?.selection ?? roomOf(d), disabled = draft?.saving ? 'disabled' : '';
  return `<div class="field"><label for="dRoom">Room</label>
    <select id="dRoom" class="inp" onchange="setRoom(${d.avion_id},this.value)" ${disabled}><option value="" ${!selected && !draft?.editing ? 'selected' : ''}>No room</option>
    ${rooms().map(r => `<option ${r === selected && !draft?.editing ? 'selected' : ''}>${esc(r)}</option>`).join('')}<option value="__new" ${draft?.editing ? 'selected' : ''}>New room…</option></select>
    ${draft?.editing ? `<div id="newRoomBox"><label for="newRoomName">New room name</label><div class="inline-new"><input id="newRoomName" class="inp" type="text" maxlength="40" value="${esc(draft.name)}" aria-describedby="roomFieldStatus" ${draft.error ? 'aria-invalid="true"' : ''} enterkeyhint="done" oninput="editNewRoom(${d.avion_id},this.value)" onkeydown="if(event.key==='Enter'&&!event.isComposing){event.preventDefault();saveNewRoom(${d.avion_id})}" ${disabled}>
      <button class="btn sm" onclick="saveNewRoom(${d.avion_id})" ${disabled}>Add room</button><button class="btn sm" onclick="cancelNewRoom(${d.avion_id})" ${disabled}>Cancel</button></div></div>` : ''}
    <span id="roomFieldStatus" class="${draft?.error ? 'err' : 'hint'}" role="status">${esc(draft?.error || (draft?.saving ? 'Saving room…' : ''))}</span>
    <span class="hint">Rooms organise this page only. Assigning a room does not add lights to a group or change wall controls. Use Groups to control several lights together.</span></div>`;
}
function nameField(d) {
  if (!feat('labels')) return '';
  const draft = deviceNameDrafts.get(d.avion_id);
  return `<div class="field"><label for="dName">Name</label><input id="dName" type="text" maxlength="80" value="${esc(draft?.value ?? d.name)}" aria-describedby="deviceNameStatus" ${draft?.error ? 'aria-invalid="true"' : ''} ${draft?.saving ? 'disabled' : ''} enterkeyhint="done" oninput="editDeviceName(${d.avion_id},this.value)" onchange="renameDevice(${d.avion_id},this.value)" onkeydown="if(event.key==='Enter'&&!event.isComposing){event.preventDefault();this.blur()}">
    <span id="deviceNameStatus" class="${draft?.error ? 'err' : 'hint'}" role="status">${esc(draft?.error || (draft?.saving ? 'Saving name…' : 'Saves when you leave this field.'))}</span>
    ${draft?.error ? `<div class="row"><button class="btn sm" onclick="renameDevice(${d.avion_id},$('#dName').value)">Save name</button><button class="btn sm" onclick="deviceNameDrafts.delete(${d.avion_id});render()">Discard changes</button></div>` : ''}</div>`;
}

function typeSourceText(d) {
  return 'Recorded when paired. The exact model isn\'t physically verified.';
}
function lightDrawer(d) {
  const p = known(d) ? pct(d.brightness) : 0;
  return `<div class="drawer-head">${glow(d)}<div class="t"><h3>${esc(d.name)}</h3><div class="small muted">${esc(typeName(d))}${roomOf(d) ? ' · ' + esc(roomOf(d)) : ''}</div></div><button class="icon-btn" aria-label="Close" onclick="closeDrawer()">${I.close}</button></div>
  <div class="drawer-body">
    ${feat('control') ? `<div class="sec"><div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap">
      <label class="switch"><input type="checkbox" ${devicePowerState(d.avion_id) ? 'checked' : ''} aria-label="Power" title="${esc(devicePowerHint(d.avion_id))}" onchange="devicePower(${d.avion_id},this.checked)"><span></span></label><b>${stateText(d)}</b>
      ${feat('flash') ? `<button class="btn sm" style="margin-left:auto" onclick="flash(${d.avion_id})">${I.flash}Flash to find it</button>` : ''}</div>
      ${canDim(d) ? `<div class="slider-lab"><label for="briR">Brightness</label><span id="briV" class="mono">${known(d) ? p + '%' : '–'}</span></div>
      <input id="briR" type="range" min="0" max="100" value="${p}" oninput="$('#briV').textContent=this.value+'%'" onchange="setBrightness(${d.avion_id},this.value)">` : ''}
      ${d.has_color_temp ? kelvinControls(d.avion_id, d.color_temp) : ''}
      ${d.color_temp_last_known ? '<p class="hint">Colour temperature is the last known reading; waiting for an update.</p>' : ''}
      ${!known(d) ? `<p class="hint">${d.power_read_status === 'no_reply' ? 'Three power checks received no reply.' : d.power_read_status === 'retry_pending' ? 'The bridge will check this light again.' : gettingStatus(d) ? 'Waiting for the first power reading.' : 'The light hasn\'t reported its state yet. Changing it will ask for a reply.'}</p>` : ''}</div>` : ''}
    <div class="sec"><h4>Name and room</h4>${nameField(d)}${roomField(d)}</div>
    ${haSharingHtml('device', d)}
    <div class="sec"><h4>Groups</h4>${(d.groups || []).length ? `<div style="display:flex;flex-wrap:wrap;gap:6px">${d.groups.map(g => `<button class="chip" onclick="openDrawer('group',${g})">${esc(gName(g))}</button>`).join('')}</div>` : '<p class="hint" style="margin:0">Not in any group.</p>'}
      ${feat('group_edit') ? '' : '<p class="hint">Changing group membership isn\'t available on this bridge yet.</p>'}</div>
    <div class="sec"><h4>Details</h4><dl class="kv"><dt>Mesh ID</dt><dd class="mono">#${d.avion_id}</dd><dt>Type</dt><dd>${esc(typeName(d))} <span class="muted mono">(${d.product_type})</span></dd>
      <dt>Type source</dt><dd>${typeSourceText(d)}</dd></dl></div>
    ${deviceToolsHtml(d)}
  </div>`;
}
function kelvinControls(id, ct) {
  const k = ct || 2700;
  return `<div class="slider-lab" style="margin-top:14px"><label for="ctR">Colour temperature</label><span id="ctV" class="mono">${ct ? ct + ' K' : '–'}</span></div>
  <input id="ctR" class="kelvin" type="range" min="2700" max="5000" step="100" value="${k}" oninput="$('#ctV').textContent=this.value+' K'" onchange="setKelvin(${id},this.value)">
  <div class="presets">${[[2700,'Warm'],[3000,'Soft white'],[3500,'Neutral'],[4000,'Cool white'],[5000,'Daylight']].map(([v, n]) => `<button onclick="setKelvin(${id},${v})" aria-pressed="${Number.isFinite(ct) && Math.abs(ct - v) <= 50}"><i style="background:${kelvinColor(v)}"></i>${n} <span class="mono muted">${v}K</span></button>`).join('')}</div>`;
}
/* ---------- keypad button editor ---------- */
// One reviewed save for all changed buttons. The bridge validates the whole set,
// then writes and reads back one button at a time. This page never retries a write.
const keypadDrafts = new Map(), keypadSaves = new Map();
const KEYPAD_PRESETS = [[255, '100%'], [191, '75%'], [128, '50%'], [64, '25%']];
const KEYPAD_STATES = {waiting:'Waiting', writing:'Writing and checking…', verified:'Saved and verified',
  not_started:'Not started', not_sent:'Not sent', needs_review:'Needs review'};
function keypadView(id) { return S.controllerViews.find(v => v.device_id === id); }
function keypadEditable(id) {
  const v = keypadView(id);
  return !!v && feat('keypad_edit') === true && v.assignment_read_back === true && v.actions?.edit_assignment?.enabled === true;
}
function keypadEditorVisible(id) {
  const v = keypadView(id);
  return !!v && feat('keypad_edit') === true && v.assignment_read_back === true &&
    (v.editor_available ?? v.actions?.edit_assignment?.enabled) === true;
}
function keypadHaAvailable(row) { return (row.ha_draft_available ?? row.ha_available) === true; }
function keypadEditorNotice(id) {
  const v = keypadView(id); if (!v?.assignment_read_back || v.actions?.edit_assignment?.enabled) return '';
  const text = {operation_in_progress:'Bridge busy. Your selections are kept; save when it is ready.',
    resolve_pending_operations:'Review unfinished operations in Operations before changing buttons.',
    mesh_offline:'The bridge is disconnected. Read the assignments again after it reconnects.',
    model_editor_disabled:'Button programming is not enabled for this keypad.'}[v.actions?.edit_assignment?.reason];
  return text ? `<p class="hint">${text}</p>` : '';
}
function keypadDraft(id) {
  const v = keypadView(id); let draft = keypadDrafts.get(id);
  // A draft belongs to one verified table. A new table revision discards it.
  if (!draft || draft.revision !== v.revision) { draft = {revision:v.revision, slots:{}}; keypadDrafts.set(id, draft); }
  return draft;
}
function keypadMode(row) {
  // Only the bridge can prove that a protected target belongs to this button.
  if (row.assignment_mode) return row.assignment_mode === 'ha' && row.ha_owned !== true ? 'unknown' : row.assignment_mode;
  return row.status === 'unused' ? 'unassigned' : (row.draft_editable ?? row.editable) === true ? 'native' : 'unknown';
}
function keypadMqttReady() { return S.status.mqtt_enabled === true && S.status.mqtt_ready === true; }
function keypadRowEditable(row) { return (row.draft_editable ?? row.editable) === true && ['native','ha','unassigned'].includes(keypadMode(row)); }
function keypadChoice(id, row) {
  return keypadDraft(id).slots[row.slot] || {mode:keypadMode(row),
    group:keypadMode(row) === 'native' ? row.target : 0,
    preset:keypadMode(row) === 'native' ? row.preset : 255};
}
function keypadChanges(id) {
  const v = keypadView(id); if (!v) return [];
  return (v.slots || []).filter(keypadRowEditable).map(row => {
    const c = keypadChoice(id, row), mode = keypadMode(row);
    if (c.mode === 'unassigned' || c.mode === 'ha') return c.mode === mode ? null : {slot:row.slot, mode:c.mode};
    if (c.mode !== 'native' || !c.group) return null;
    if (mode === 'native' && c.group === row.target && c.preset === row.preset) return null;
    const change = {slot:row.slot, mode:'native', group:c.group};
    // Send a level only when it is a deliberate choice, so a saved raw level is never rounded.
    if (mode !== 'native' || c.group !== row.target ? c.preset !== 255 : c.preset !== row.preset) change.brightness = c.preset;
    return change;
  }).filter(Boolean);
}
function keypadDuplicate(id) {
  const seen = new Set();
  for (const row of keypadView(id)?.slots || []) {
    const c = keypadRowEditable(row) ? keypadChoice(id, row) : null;
    const g = c ? c.mode === 'native' ? c.group : 0 : row.status === 'unused' ? 0 : row.target;
    if (g && seen.has(g)) return g;
    if (g) seen.add(g);
  }
  return 0;
}
function setKeypadChoice(id, slot, key, value) {
  const row = keypadView(id)?.slots?.find(r => r.slot === slot);
  if (!row || !keypadRowEditable(row) || keypadSaves.get(id)?.busy || keypadSaves.get(id)?.review) return;
  const c = {...keypadChoice(id, row)};
  if (key === 'mode') {
    if (!['native','ha','unassigned'].includes(value) || value === 'ha' && (!keypadHaAvailable(row) || !keypadMqttReady())) return;
    c.mode = value;
  } else if (key === 'group') {
    c.group = Number(value); c.mode = c.group ? 'native' : 'unassigned';
  } else if (key === 'preset') c.preset = Number(value);
  else return;
  keypadDraft(id).slots[slot] = c;
  keypadSaves.delete(id); renderSoon();
}
function keypadSaveAllowed(id) {
  return keypadEditable(id) && managementFresh() && !S.radio.label && !S.radio.queue.length && !groupBusy &&
    !keypadSaves.get(id)?.busy && !keypadSaves.get(id)?.review && keypadChanges(id).length > 0 && !keypadDuplicate(id) &&
    !(keypadView(id)?.slots || []).some(row => keypadRowEditable(row) &&
      (keypadChoice(id,row).mode === 'native' && !keypadChoice(id,row).group ||
       keypadChoice(id,row).mode === 'ha' && keypadMode(row) !== 'ha' && (!keypadHaAvailable(row) || !keypadMqttReady())));
}
function keypadAssignmentText(row) {
  const mode = keypadMode(row);
  return mode === 'unassigned' ? 'Unused' : mode === 'ha' && row.ha_owned === true ? 'Home Assistant' :
    mode === 'native' ? `${gName(row.target)} at ${pct(row.preset)}%` : `Protected or unknown assignment · ${gName(row.target)}`;
}
function keypadChangeText(id, change) {
  const row = keypadView(id).slots.find(r => r.slot === change.slot);
  const from = keypadAssignmentText(row);
  const level = change.brightness ?? (change.group === row.target ? row.preset : 255);
  return `Button ${change.slot}: ${from} → ${change.mode === 'unassigned' ? 'Unused' : change.mode === 'ha' ? 'Home Assistant (automation required)' : `${gName(change.group)} at ${pct(level)}%`}`;
}
async function saveKeypad(id) {
  if (!keypadSaveAllowed(id)) return;
  const v = keypadView(id), changes = keypadChanges(id);
  if (!confirm(`Save these button changes to ${dev(id)?.name || 'the keypad'}?\n\n${changes.map(c => keypadChangeText(id, c)).join('\n')}\n\nEach button is written once and then read back. Nothing is retried automatically.`)) return;
  keypadSaves.set(id, {busy:true, message:'Sending changes…', job:null, instance:S.status.instance}); renderSoon();
  try {
    const result = await api('/api/keypad/save', {target:id, revision:v.revision, changes});
    keypadSaves.set(id, {busy:true, message:'Save accepted. Writing and checking each button.', job:result.job_id, instance:S.status.instance});
  } catch (e) {
    // Only the bridge's explicit refusals prove that nothing was sent.
    const refused = e.details?.operation_started === false || e.status === 400 || e.status === 501;
    keypadSaves.set(id, refused
      ? {busy:false, review:e.details?.requires_review === true, receipt:e.details?.transaction, message:'Save not started. ' + e.message + (e.status === 400 ? ' Read the buttons again and retry.' : '')}
      : {busy:false, review:true, receipt:e.details?.transaction, message:'Save outcome not confirmed. ' + e.message});
  }
  await refreshSnapshot(); renderSoon();
}
function keypadSaveStatus(id) {
  const save = keypadSaves.get(id); if (!save) return '';
  const job = S.status?.jobs?.[id], progress = save.job ? S.status?.keypad_saves?.[id] : null;
  if (save.busy && save.job && save.instance !== S.status.instance) {
    save.busy = false; save.review = true;
    save.message = 'The bridge restarted during the save. Read the buttons again to see what was stored.';
  } else if (save.busy && save.job && job?.id === save.job && job.state !== 'running') {
    save.busy = false;
    if (job.state === 'completed') {
      save.message = 'All changes saved and verified by reading the keypad back.'; keypadDrafts.delete(id);
    } else if (job.state === 'not_started' && job.operation_started === false) {
      save.message = 'Save not started. ' + (job.message || '');
    } else if (progress?.state === 'stopped') {
      save.message = 'Save stopped before the next button was sent. Read the buttons again, then retry.';
    } else {
      save.review = true;
      save.message = 'A button change could not be confirmed. Nothing is retried automatically.';
    }
  }
  const rows = progress ? progress.buttons.map(b => `<li>Button ${esc(b.slot)}: ${esc(KEYPAD_STATES[b.state] || b.state)}</li>`).join('') : '';
  return `<div role="status"><p>${esc(save.message)}</p>${rows ? `<ul>${rows}</ul>` : ''}${save.review ? '<p>' + opsReviewButton(save.receipt) + '</p>' : ''}</div>`;
}
function keypadEditorHtml(d, v) {
  const id = d.avion_id, busy = keypadSaves.get(id)?.busy === true || keypadSaves.get(id)?.review === true, dup = keypadDuplicate(id);
  const groups = lightingGroups().filter(g => g.group_id >= 256 && g.group_id <= 24575).sort((a, b) => a.name.localeCompare(b.name));
  const rows = (v.slots || []).map(row => {
    if (!keypadRowEditable(row)) return `<div class="kp-row"><span class="kp-n">Button ${esc(row.slot)}</span><span class="muted">${esc(keypadAssignmentText(row))} (cannot be changed here)</span></div>`;
    const c = keypadChoice(id, row);
    const options = groups.some(g => g.group_id === c.group) || !c.group ? groups : [{group_id:c.group, name:gName(c.group)}, ...groups];
    const presets = KEYPAD_PRESETS.some(([raw]) => raw === c.preset) ? KEYPAD_PRESETS : [[c.preset, pct(c.preset) + '% (saved)'], ...KEYPAD_PRESETS];
    return `<div class="kp-row"><label class="kp-n" for="kpM${row.slot}">Button ${esc(row.slot)}</label>
      <select id="kpM${row.slot}" class="inp kp-mode" aria-label="Button ${esc(row.slot)} control" onchange="setKeypadChoice(${id},${row.slot},'mode',this.value)" ${busy ? 'disabled' : ''}>
        <option value="native" ${c.mode === 'native' ? 'selected' : ''}>Direct group</option>
        <option value="ha" ${c.mode === 'ha' ? 'selected' : ''} ${keypadHaAvailable(row) && keypadMqttReady() ? '' : 'disabled'}>Home Assistant</option>
        <option value="unassigned" ${c.mode === 'unassigned' ? 'selected' : ''}>Unused</option></select>
      ${c.mode === 'native' ? `<select id="kpG${row.slot}" class="inp kp-group" aria-label="Button ${esc(row.slot)} group" onchange="setKeypadChoice(${id},${row.slot},'group',this.value)" ${busy ? 'disabled' : ''}><option value="0" ${c.group ? '' : 'selected'} disabled>Choose a group</option>${options.map(g => `<option value="${g.group_id}" ${g.group_id === c.group ? 'selected' : ''}>${esc(g.name)}</option>`).join('')}</select>
      <select id="kpL${row.slot}" class="inp kp-level" aria-label="Button ${esc(row.slot)} brightness" onchange="setKeypadChoice(${id},${row.slot},'preset',this.value)" ${busy || !c.group ? 'disabled' : ''}>${presets.map(([raw, label]) => `<option value="${raw}" ${raw === c.preset ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select>` : c.mode === 'ha' ? '<span class="kp-note hint">Uses a Home Assistant automation. No direct light control.</span>' : ''}</div>`;
  }).join('');
  const n = keypadChanges(id).length;
  return `<div class="kp-edit">${rows}
    ${dup ? `<p class="hint">${esc(gName(dup))} is chosen for more than one button. Each button needs its own group.</p>` : ''}
    <p><button class="btn primary" onclick="saveKeypad(${id})" ${keypadSaveAllowed(id) ? '' : 'disabled'}>${n ? `Save ${n} change${n === 1 ? '' : 's'}` : 'Save changes'}</button></p>
    ${d.product_type === 126 ? '<p class="hint">Battery keypad: follow each wake prompt while reading and saving. Several wakes may be needed per button.</p>' : ''}
    ${!keypadMqttReady() ? '<p class="hint">Connect MQTT in Settings to choose Home Assistant for a button.</p>' : ''}
    <p class="hint">Direct group works without Home Assistant. To control a single light directly, put it in its own group first.</p>
    <p class="hint">Home Assistant requires MQTT and a Home Assistant automation for the button event. Choose the lights and brightness in that automation. Use Add to Home Assistant below to share this keypad’s events; sharing alone does not change its button assignments.</p></div>`;
}
/* ---------- Accessory Dimmer setup ---------- */
const accessoryRequests = new Map();
let accessoryReview = null;
const ACCESSORY_REASONS = {
  model_setup_disabled:'Accessory Dimmer HA setup is not enabled on this bridge.',
  model_editor_disabled:'Direct dimmer assignment is not enabled on this bridge.',
  retained_dimmer_claim_required:'This controller needs a verified pairing record from this bridge before its control can be changed.',
  fresh_retained_claim_required:'HA setup requires a newly paired dimmer and can only run once. Review the previous setup in Operations.',
  authoritative_inventory_required:'The bridge needs a complete device inventory before setup.',
  fresh_native_baseline_required:'Press the dimmer to observe its current target before reviewing a direct group change.',
  protected_native_source:'This dimmer uses a managed HA target. Changing it to direct control requires a supported recovery workflow.',
  ordinary_destination_required:'Create a lighting group different from the current target first.',
  resolve_pending_operations:'Review unfinished operations first, under Settings → Operations.',
  operation_in_progress:'Another bridge operation is running.', mesh_offline:'The bridge is disconnected.'
};
function accessorySetupProgress(id) {
  const job = S.status?.jobs?.[id], p = job?.progress;
  if (!managementFresh() || S.status?.mesh_ready !== true || dev(id)?.product_type !== 91 || job?.state !== 'running' || !p ||
      typeof job.id !== 'string' || !job.id || p.job_id !== job.id || p.target !== id || p.product_id !== 91 ||
      typeof p.instance !== 'string' || p.instance !== S.status.instance ||
      !['accessory_setup','accessory_native_setup','accessory_recovery'].includes(p.operation) ||
      !['preparing','writing','awaiting_input','observed'].includes(p.phase)) return null;
  return p;
}
const accessoryPromptTimers = new Map();
function expireAccessoryPrompt(id) {
  clearTimeout(accessoryPromptTimers.get(id));
  accessoryPromptTimers.set(id, setTimeout(() => {
    accessoryPromptTimers.delete(id);
    if (accessorySetupProgress(id)) { expireAccessoryPrompt(id); return; }
    // Open text fields deliberately defer drawer rendering. Remove only this
    // stale instruction so it cannot keep asking for input after status expires.
    document.querySelectorAll(`[data-accessory-progress="${id}"]`).forEach(node => node.remove());
    renderSoon();
  }, Math.max(1, 15000 - (Date.now() - snapshotAt))));
}
function accessorySetupPrompt(id) {
  const progress = accessorySetupProgress(id);
  if (!progress) {
    if (accessoryPromptTimers.has(id)) { clearTimeout(accessoryPromptTimers.get(id)); accessoryPromptTimers.delete(id); }
    return '';
  }
  expireAccessoryPrompt(id);
  const recovery = progress.operation === 'accessory_recovery';
  const text = {
    preparing:[recovery ? 'Preparing the check…' : 'Preparing setup…', 'Wait for the button prompt. Do not press the dimmer yet.'],
    writing:['Sending the assignment…', 'Wait for the button prompt. Do not press the dimmer yet.'],
    awaiting_input:['Press On once now', 'Press and release On on this dimmer, then wait. The bridge is listening for its target.'],
    observed:['Input received', recovery ? 'Finishing the check. Do not press again yet.' : 'Finishing setup. Do not press again yet.']
  }[progress.phase];
  return `<div class="callout info" data-accessory-progress="${id}" role="status" aria-live="polite">${I.info}<div><strong>${esc(text[0])}</strong><p>${esc(text[1])}</p></div></div>`;
}
function updateAccessorySetupPrompts() {
  document.querySelectorAll('[data-accessory-progress-host]').forEach(host => {
    const id = Number(host.dataset.accessoryProgressHost), html = accessorySetupPrompt(id);
    if (host.innerHTML !== html) host.innerHTML = html;
  });
}
function accessoryAllowed(id, action) {
  const v = keypadView(id), request = accessoryRequests.get(id);
  const model = dev(id)?.product_type;
  return [81,91].includes(model) && managementFresh() && S.status.mesh_ready === true &&
    !S.status.pending_transactions && !S.radio.label && !S.radio.queue.length && !groupBusy &&
    ![...accessoryRequests.values(), ...batterySetups.values()].some(r => r.pending) && !request?.review &&
    !Object.values(S.status.jobs || {}).some(j => j.state === 'running') &&
    v?.actions?.[action]?.enabled === true &&
    (action !== 'setup_ha' || !request?.completed && S.status.mqtt_enabled === true && S.status.mqtt_ready === true &&
      (model !== 81 || v.anyplace_ha?.state === 'not_configured'));
}
function settleAccessoryRequests() {
  for (const [id, r] of accessoryRequests) {
    const configuration = keypadView(id)?.control_configuration;
    if (r.action === 'configure_control' && r.review && r.transaction && configuration?.state === 'configured' &&
        configuration.transaction === r.transaction && configuration.mode === r.mode && configuration.group === r.destination) {
      Object.assign(r, {review:false, completed:true, message:'Control change accepted. Test On, Off and dimming.'});
    }
    if (!r.pending || !r.job) continue;
    if (r.instance !== S.status.instance) {
      Object.assign(r, {pending:false, review:true, message:'The bridge restarted during setup. Review the recorded operation in Operations.'});
      continue;
    }
    const j = S.status.jobs?.[id];
    if (!j || j.id !== r.job || j.state === 'running') continue;
    // Only this exact job can identify its retained operation. The latest
    // controller receipt alone must never unlock an unrelated uncertain write.
    if (r.action === 'configure_control' && dev(id)?.product_type === r.model &&
        typeof j.result?.transaction === 'string' && j.result.transaction.length > 0 &&
        j.result.mode === r.mode && j.result.group === r.destination) r.transaction = j.result.transaction;
    if (r.action === 'configure_control' && j.state === 'completed' && j.result?.acceptance_required === true &&
        j.result.desired_behavior_observed === true && typeof j.result.transaction === 'string' &&
        j.result.mode === r.mode && j.result.group === r.destination) {
      Object.assign(r, {pending:false, review:true, transaction:j.result.transaction,
        message:'The new control target was observed. Stop pressing the dimmer and accept this result promptly in Operations. Then test On, Off and dimming. Nothing is sent again automatically.'});
      continue;
    }
    const anyplace = r.model === 81;
    const configured = anyplace ? keypadView(id)?.anyplace_ha : keypadView(id)?.accessory_ha;
    const ownedSetup = r.action === 'setup_ha' && j.state === 'completed' && j.result &&
        typeof j.result.transaction === 'string' && j.result.transaction.length > 0 &&
        dev(id)?.product_type === r.model && configured?.state === 'configured' && configured.transaction === j.result.transaction &&
        (!anyplace || j.result.native_behavior_observed === true && j.result.assignment_read_back === false &&
          j.result.original_assignment === 'unknown' && configured.experimental === true);
    const direct = keypadView(id)?.accessory_native;
    const nativeSetup = r.action === 'setup_native' && r.model === 91 && dev(id)?.product_type === 91 && j.state === 'completed' &&
      typeof j.result?.transaction === 'string' && j.result.transaction.length > 0 &&
      j.result.mode === 'native' && j.result.group === r.group && j.result.assignment_read_back === false &&
      j.result?.ready === true && j.result.native_behavior_observed === true &&
      direct?.state === 'configured' && direct.transaction === j.result.transaction && direct.target === r.group;
    if (nativeSetup) {
      Object.assign(r, {pending:false, completed:true, review:false,
        message:'Direct group setup completed. Test On, Off and held dimming; this control works without Home Assistant.'});
    } else if (ownedSetup && j.result.ready === true) {
      Object.assign(r, {pending:false, completed:true, review:false, message:anyplace
        ? 'Experimental setup recorded: a command to this Anyplace dimmer’s owned HA target was observed. Stored assignments were not read back. Home Assistant receipt and physical isolation still need testing.'
        : 'HA setup completed: the bridge observed a command to this dimmer’s dedicated HA target. Test the Home Assistant automation next.'});
    } else if (anyplace && ownedSetup && j.result.publication_pending === true && j.result.requires_review === true) {
      Object.assign(r, {pending:false, completed:true, review:true,
        message:'Observed assignment behavior was saved, but the publication refresh needs review. Do not repeat setup. Review Operations before testing Home Assistant.'});
    } else if (j.state === 'not_started' && operationNotStarted(j)) {
      Object.assign(r, {pending:false, review:false, message:'Setup did not start. '+(j.message || 'Refresh and try again when the bridge is ready.')});
    } else {
      Object.assign(r, {pending:false, review:true, message:j.result?.acceptance_required
        ? 'The new direct target was observed. Complete the explicit acceptance in Operations.'
        : 'Setup is not confirmed. Review it in Operations. No change will be retried automatically.'});
    }
  }
}
function openAccessorySetup(id) {
  if (dev(id)?.product_type !== 91 || !accessoryAllowed(id, 'setup_ha')) return;
  accessoryReview = {id, action:'setup_ha', instance:S.status.instance};
  modal(`<h3>Set up ${esc(dev(id).name)} for Home Assistant?</h3>
    <p>This assigns a dedicated HA target and shares this dimmer’s events. Choose which lights it controls in a Home Assistant automation.</p>
    <p>Have the dimmer nearby. Start setup, then wait for “Press On once now” in the dimmer details before pressing and releasing On. Allow up to two minutes.</p>
    <p>This setup is for a factory-reset spare paired with this bridge. Existing assignments cannot be read or backed up.</p>
    <div class="row"><button class="btn primary" onclick="confirmAccessorySetup()">Start HA setup</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
}
function confirmAccessorySetup() {
  const r = accessoryReview;
  if (!r || dev(r.id)?.product_type !== 91 || !$('#modalRoot').innerHTML || r.action !== 'setup_ha' || r.instance !== S.status.instance || !accessoryAllowed(r.id, r.action)) {
    closeModal(); toast('Bridge status changed. Review setup again.'); return;
  }
  closeModal(); accessoryReview = null;
  accessoryRequest(r.id, r.action, '/api/controller/setup', {target:r.id});
}
async function accessoryRequest(id, action, path, body) {
  if (!accessoryAllowed(id, action)) return;
  const r = {action, mode:body.mode, destination:action === 'configure_control' ? dimmerControlReview?.preview?.group : body.group, group:body.group, model:dev(id).product_type, pending:true, review:false, job:null, instance:S.status.instance,
    message:'Starting setup… Do not repeat this request.'};
  accessoryRequests.set(id, r); renderSoon();
  try {
    const result = await api(path, body);
    if (result.accepted !== true || typeof result.job_id !== 'string' || !result.job_id || r.instance !== S.status.instance) throw Error('Setup acceptance was not confirmed.');
    r.job = result.job_id;
    r.message = action === 'configure_control'
      ? r.model === 81
        ? 'Wake the Anyplace dimmer now: press the bottom Wake-Up button once, then tap On once and release. Stop pressing and accept the observed result promptly in Operations.'
        : 'Waiting for the dimmer. Tap On once and release, then stop pressing. Accept the observed result promptly in Operations.'
      : r.model === 81
      ? action === 'setup_ha' ? 'Experimental setup accepted. Follow the wake prompt below; do not start another setup.'
        : 'Wake the Anyplace dimmer now: press the small Wake-Up button on the bottom once, then use On, Off or the slider to observe its target. If it times out, review the operation before another change.'
      : ['setup_ha','setup_native'].includes(action) ? 'Setup accepted. Wait for the button prompt below; do not press the dimmer yet.'
        : 'Waiting for the dimmer. Press On or adjust its brightness now. The bridge will observe the target; allow up to two minutes.';
  } catch (e) {
    Object.assign(r, {pending:false, review:!operationNotStarted(e.details),
      message:operationNotStarted(e.details) ? 'Setup did not start. '+e.message
        : 'Setup outcome is not confirmed. Review Operations before another change. '+e.message});
  }
  // A failed snapshot must never erase the accepted job or permit another write.
  await refreshSnapshot(); settleAccessoryRequests(); renderSoon();
}
function openAccessoryNativeSetup(id) {
  if (dev(id)?.product_type !== 91 || !accessoryAllowed(id, 'setup_native')) return;
  const groups = lightingGroups().filter(g => g.members.length);
  accessoryReview = {id, action:'setup_native', instance:S.status.instance};
  modal(`<h3>Set up direct group control</h3><p>${esc(dev(id).name)} will control the selected lights directly, even when Home Assistant is offline.</p>
    <label for="accessoryGroup">Lighting group</label><select id="accessoryGroup"><option value="">Choose a group</option>${groups.map(g => `<option value="${g.group_id}">${esc(g.name)}</option>`).join('')}</select>
    <p>For a newly paired dimmer. Start setup, then wait for “Press On once now” in the dimmer details before pressing and releasing On. Allow up to two minutes. An uncertain change will not be sent again.</p>
    <div class="row"><button class="btn primary" onclick="confirmAccessoryNativeSetup()">Start direct setup</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
}
function confirmAccessoryNativeSetup() {
  const r = accessoryReview, group = Number($('#accessoryGroup')?.value);
  if (!r || r.action !== 'setup_native' || r.instance !== S.status.instance ||
      !accessoryAllowed(r.id, r.action) || !lightingGroups().some(g => g.group_id === group && g.members.length)) {
    toast('Choose a lighting group and check that the bridge is ready.'); return;
  }
  closeModal(); accessoryReview = null;
  accessoryRequest(r.id, r.action, '/api/controller/setup-native', {target:r.id, group});
}
function openAccessoryNative(id) {
  if (!accessoryAllowed(id, 'assign_native_target')) return;
  const observed = keypadView(id)?.observed_target;
  if (!observed?.baseline || !Number.isFinite(observed.valid_for_ms)) return;
  const groups = S.groups.filter(g => !g.protected && g.group_id >= 256 && g.group_id <= 24575 && g.group_id !== observed.target);
  accessoryReview = {id, action:'assign_native_target', instance:S.status.instance,
    baseline:{...observed.baseline}, deadline:snapshotAt + observed.valid_for_ms};
  modal(`<h3>Assign a direct lighting group</h3><p>Pressing this dimmer will control the selected group without Home Assistant.</p>
    ${dev(id)?.product_type === 81 ? '<p>Have the Anyplace dimmer ready. Immediately after confirming, press the small Wake-Up button on the bottom once. It has a short programming window.</p>' : ''}
    <label for="accessoryGroup">Lighting group</label><select id="accessoryGroup"><option value="">Choose a group</option>${groups.map(g => `<option value="${g.group_id}">${esc(g.name)}</option>`).join('')}</select>
    <p>The current target was observed, not read from a stored assignment table. The review expires after 15 seconds; a completed change needs explicit acceptance in Operations.</p>
    <div class="row"><button class="btn primary" onclick="confirmAccessoryNative()">Assign group</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
}
function confirmAccessoryNative() {
  const r = accessoryReview, group = Number($('#accessoryGroup')?.value), v = r && keypadView(r.id);
  if (!r || r.action !== 'assign_native_target' || r.instance !== S.status.instance ||
      !accessoryAllowed(r.id, r.action) || Date.now() >= r.deadline ||
      JSON.stringify(v?.observed_target?.baseline) !== JSON.stringify(r.baseline) ||
      !S.groups.some(g => !g.protected && g.group_id === group && group >= 256 && group <= 24575 && group !== r.baseline.target)) {
    toast('Choose a group using a fresh dimmer observation. Reopen the review if it expired.'); return;
  }
  closeModal(); accessoryReview = null;
  accessoryRequest(r.id, r.action, '/api/dimmer/assign', {target:r.id, group, baseline:r.baseline});
}
/* ---------- change an existing dimmer's control mode ---------- */
let dimmerControlReview = null;
function dimmerControlChoice() {
  const mode = $('#dimmerControlMode')?.value, group = Number($('#dimmerControlGroup')?.value);
  return {mode, ...(mode === 'native' ? {group} : {})};
}
function dimmerControlReady(id, mode) {
  return accessoryAllowed(id, 'configure_control') &&
    (mode !== 'ha' || S.status.mqtt_enabled === true && S.status.mqtt_ready === true);
}
function sameDimmerBaseline(a, b) {
  return !!a && !!b && a.target === b.target && a.observed_ms === b.observed_ms && a.target_generation === b.target_generation;
}
function dimmerControlReviewValid(r) {
  const choice = dimmerControlChoice(), v = r && keypadView(r.id);
  return !!r?.preview && dimmerControlReview === r && r.instance === S.status.instance &&
    dev(r.id)?.product_type === r.model && dimmerControlReady(r.id, choice.mode) &&
    choice.mode === r.choice.mode && choice.group === r.choice.group && Date.now() < r.deadline &&
    sameDimmerBaseline(v?.observed_target?.baseline, r.preview.baseline) &&
    (choice.mode === 'ha' || lightingGroups().some(g => g.group_id === choice.group && g.members.length));
}
function updateDimmerControlReview() {
  const r = dimmerControlReview, status = $('#dimmerControlStatus'), mode = $('#dimmerControlMode');
  if (!r || !status || !mode) return;
  const choice = dimmerControlChoice(), groupRow = $('#dimmerControlGroupRow');
  if (groupRow) groupRow.hidden = choice.mode !== 'native';
  const observed = keypadView(r.id)?.observed_target;
  const current = r.instance === S.status.instance && dev(r.id)?.product_type === r.model;
  let message = r.message || (observed?.baseline && observed.valid_for_ms > 0
    ? 'Recent input received. Review your choice, then confirm while the observation is fresh.'
    : 'Tap On once, release it, then choose Review change. This observes the current target; it does not change the assignment.');
  if (!current) message = 'The bridge restarted. Close this form and review the control again.';
  else if (choice.mode === 'ha' && !keypadMqttReady()) message = 'Connect MQTT in Settings before choosing Home Assistant.';
  else if (r.preview && !dimmerControlReviewValid(r)) message = 'The review changed or expired. Tap On once, then review again. No change has been sent.';
  status.textContent = message;
  const previewButton = $('#dimmerControlPreview'), applyButton = $('#dimmerControlApply');
  if (previewButton) previewButton.disabled = r.busy || !current || !dimmerControlReady(r.id, choice.mode) ||
    !['native', 'ha'].includes(choice.mode) || choice.mode === 'native' && !lightingGroups().some(g => g.group_id === choice.group && g.members.length);
  if (applyButton) applyButton.disabled = r.busy || !dimmerControlReviewValid(r);
}
function changeDimmerControlChoice() {
  if (!dimmerControlReview) return;
  dimmerControlReview.preview = null; dimmerControlReview.message = '';
  updateDimmerControlReview();
}
function openDimmerControl(id) {
  if (!accessoryAllowed(id, 'configure_control')) return;
  const current = keypadView(id)?.control_configuration, model = dev(id).product_type;
  const mode = current?.mode === 'ha' ? 'ha' : 'native';
  const groups = lightingGroups().filter(g => g.members.length).sort((a, b) => a.name.localeCompare(b.name));
  dimmerControlReview = {id, model, instance:S.status.instance, preview:null, busy:false};
  modal(`<h3>Choose what ${esc(dev(id).name)} controls</h3>
    <label for="dimmerControlMode">Control mode</label><select id="dimmerControlMode" class="inp" onchange="changeDimmerControlChoice()">
      <option value="native" ${mode === 'native' ? 'selected' : ''}>Direct lighting group</option>
      <option value="ha" ${mode === 'ha' ? 'selected' : ''}>Home Assistant</option></select>
    <div id="dimmerControlGroupRow"><label for="dimmerControlGroup">Lighting group</label><select id="dimmerControlGroup" class="inp" onchange="changeDimmerControlChoice()"><option value="">Choose a group</option>${groups.map(g => `<option value="${g.group_id}" ${current?.mode === 'native' && current.group === g.group_id ? 'selected' : ''}>${esc(g.name)}</option>`).join('')}</select></div>
    <p>Direct group controls these lights without Home Assistant. Home Assistant control uses a dedicated event target; choose the lights in a Home Assistant automation. Switching to direct control does not disable an existing HA automation.</p>
    <p>Tap On once and release it before reviewing. ${model === 81 ? 'After confirming, press the small Wake-Up button on the bottom once immediately, then tap On once and release.' : 'After confirming, tap On once and release.'} Stop pressing and accept the observed result promptly in Operations.</p>
    <p id="dimmerControlStatus" role="status"></p>
    <p class="hint">A review lasts at most 15 seconds. The bridge checks observed input; stored assignment readback is unavailable. An uncertain write is never repeated automatically.</p>
    <div class="row"><button id="dimmerControlPreview" class="btn" onclick="previewDimmerControl()">Review change</button>
      <button id="dimmerControlApply" class="btn primary" onclick="confirmDimmerControl()" disabled>Confirm change</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`);
  updateDimmerControlReview();
}
async function previewDimmerControl() {
  const r = dimmerControlReview, choice = dimmerControlChoice();
  if (!r || r.busy || !$('#dimmerControlMode') || r.instance !== S.status.instance || !dimmerControlReady(r.id, choice.mode)) return;
  if (!['ha','native'].includes(choice.mode) || choice.mode === 'native' && !lightingGroups().some(g => g.group_id === choice.group && g.members.length)) return;
  r.busy = true; r.preview = null; r.message = 'Checking the current target and your choice…'; updateDimmerControlReview();
  const started = Date.now();
  try {
    const result = await api('/api/dimmer/preview', {target:r.id, ...choice});
    if (dimmerControlReview !== r || !$('#dimmerControlMode') || r.instance !== S.status.instance || dev(r.id)?.product_type !== r.model)
      throw Error('The controller or bridge changed. Close and review again.');
    const latestChoice = dimmerControlChoice();
    if (latestChoice.mode !== choice.mode || latestChoice.group !== choice.group) throw Error('Your selection changed. Review it again.');
    if (result.target !== r.id || result.mode !== choice.mode || !Number.isInteger(result.group) || result.group < 256 || result.group > 24575 ||
        choice.mode === 'native' && result.group !== choice.group || typeof result.revision !== 'string' || !/^[a-f0-9]{64}$/.test(result.revision) ||
        !result.baseline || !Number.isFinite(result.valid_for_ms) || result.valid_for_ms <= 0)
      throw Error('The bridge did not return a valid control review.');
    r.preview = result; r.choice = choice; r.deadline = started + Math.min(15000, result.valid_for_ms);
    r.message = choice.mode === 'native' ? `Reviewed: direct control of ${gName(choice.group)}. Confirm now, then test the dimmer.`
      : 'Reviewed: Home Assistant control. An HA automation is required; this change does not create one.';
  } catch (e) { r.preview = null; r.message = 'No change sent. ' + e.message + ' Tap On once and review again when ready.'; }
  finally { r.busy = false; if (dimmerControlReview === r) updateDimmerControlReview(); }
}
function confirmDimmerControl() {
  const r = dimmerControlReview;
  if (!r || r.busy || !$('#dimmerControlMode') || !dimmerControlReviewValid(r)) { updateDimmerControlReview(); return; }
  const body = {target:r.id, ...r.choice, baseline:{...r.preview.baseline}, revision:r.preview.revision};
  closeModal();
  accessoryRequest(r.id, 'configure_control', '/api/dimmer/configure', body);
}
function dimmerControlDetails(d, v) {
  const id = d.avion_id, current = v.control_configuration, request = accessoryRequests.get(id);
  const phase = d.product_type === 81 ? anyplacePhaseText(id) : '';
  const setupPrompt = d.product_type === 91 ? accessorySetupPrompt(id) : '';
  const status = current.state === 'needs_review' ? `<p>A control change needs review before another change.</p>${opsReviewButton(current.transaction)}`
    : current.configured && current.mode === 'native' ? `<p><strong>Direct group control: ${esc(gName(current.group))}</strong></p><p>On, Off and brightness control this group without Home Assistant.</p>`
    : current.configured && current.mode === 'ha' ? '<p><strong>Home Assistant control</strong></p><p>This dimmer sends events to its dedicated HA target. Its Home Assistant automation chooses the lights; MQTT and HA must be running.</p>'
    : '<p>No accepted control assignment is recorded yet.</p>';
  const fresh = !current.configured && current.state !== 'needs_review';
  return `<div class="sec"><h4>${d.product_type === 81 ? 'Anyplace dimmer' : 'Accessory Dimmer'} control</h4>${d.product_type === 91 ? `<div data-accessory-progress-host="${id}">${setupPrompt}</div>` : ''}${status}
    ${fresh ? `<div class="row">${d.product_type === 91 ? `<button class="btn" onclick="openAccessoryNativeSetup(${id})" ${accessoryAllowed(id,'setup_native') ? '' : 'disabled'}>Set up direct group</button>` : ''}
      <button class="btn" onclick="${d.product_type === 81 ? 'openAnyplaceSetup' : 'openAccessorySetup'}(${id})" ${accessoryAllowed(id,'setup_ha') ? '' : 'disabled'}>Set up Home Assistant control</button></div>` : ''}
    <button class="btn primary" onclick="openDimmerControl(${id})" ${accessoryAllowed(id,'configure_control') ? '' : 'disabled'}>${current.configured ? 'Change control mode or group' : 'Choose control from observed input'}</button>
    ${v.actions?.configure_control?.enabled !== true ? `<p class="hint">${esc(ACCESSORY_REASONS[v.actions?.configure_control?.reason] || 'Control editing is not available for this controller yet.')}</p>` : ''}
    ${phase ? `<p role="status">${esc(phase)}</p>` : ''}
    ${request && !(request.pending && setupPrompt) ? `<p role="status">${esc(request.message)}</p>${request.review ? opsReviewButton(request.transaction) : ''}` : ''}
    <p class="hint">${setupPrompt ? 'Follow the setup instructions above. Stored assignment readback is unavailable.' : 'After a change, tap On once and release. Stop pressing and accept the observed target promptly in Operations, then test On, Off and dimming. Stored assignment readback is unavailable.'}</p></div>`;
}

function accessoryDetails(d, v) {
  if (v.control_configuration) return dimmerControlDetails(d, v);
  const id = d.avion_id, configured = v.accessory_ha?.state === 'configured', r = accessoryRequests.get(id);
  const direct = v.accessory_native, directConfigured = direct?.state === 'configured';
  const reason = v.actions?.setup_ha?.reason, setupPrompt = accessorySetupPrompt(id);
  const mqtt = S.status.mqtt_enabled === true && S.status.mqtt_ready === true;
  return `<div class="sec"><h4>Accessory Dimmer control</h4><div data-accessory-progress-host="${id}">${setupPrompt}</div>
    ${directConfigured ? `<p><strong>Direct group control: ${esc(gName(direct.target))}</strong></p><p>On, Off and brightness control this group without Home Assistant.</p>`
      : direct?.state === 'needs_review' ? `<p>Direct setup needs review.</p>${opsReviewButton(direct.transaction)}`
      : configured ? `<p><strong>Home Assistant target configured</strong></p><p>Target <span class="mono">${esc(v.accessory_ha.target)}</span> · ${v.accessory_ha.observed_in_session ? 'Matching input observed recently.' : 'Press the dimmer to check input after reconnect.'}</p>
      <p>Use this dimmer’s brightness event in a Home Assistant automation to control the lights. HA and MQTT must be running.</p>`
      : v.accessory_ha?.state === 'needs_review' ? '<p>Previous setup needs review in Operations.</p>'
      : `<button class="btn primary" onclick="openAccessorySetup(${id})" ${accessoryAllowed(id,'setup_ha') ? '' : 'disabled'}>Set up Home Assistant control</button>
      <p class="hint">${esc(!mqtt ? 'Connect MQTT in Settings before HA setup.' : ACCESSORY_REASONS[reason] || 'For a newly paired, factory-reset spare. You will need to press the dimmer during setup.')}</p>`}
    ${r && !(r.pending && setupPrompt) ? `<div role="status" aria-live="polite"><p>${esc(r.message)}</p>${r.review ? opsReviewButton(r.transaction) : ''}</div>` : ''}
    ${!configured && !directConfigured && direct?.state !== 'needs_review' ? `<div><p>Direct group control works even when HA is offline.</p><button class="btn" onclick="${v.actions?.setup_native?.enabled ? 'openAccessoryNativeSetup' : 'openAccessoryNative'}(${id})" ${accessoryAllowed(id,'setup_native') || accessoryAllowed(id,'assign_native_target') ? '' : 'disabled'}>Choose direct group</button><p class="hint">${esc(v.actions?.setup_native?.enabled ? 'Choose a group, then press On when prompted.' : ACCESSORY_REASONS[v.actions?.assign_native_target?.reason] || 'Direct setup is unavailable for this controller.')}</p></div>` : ''}
    <p class="hint">On, Off and brightness describe observed input. Held dimming and restart behavior need a physical test. Stored assignment readback and clear are unavailable.</p></div>`;
}

function openAnyplaceSetup(id) {
  if (dev(id)?.product_type !== 81 || !accessoryAllowed(id, 'setup_ha')) return;
  accessoryReview = {id, action:'setup_ha', model:81, instance:S.status.instance};
  modal(`<h3>Experimental Anyplace HA setup</h3>
    <p>Use this only for a factory-reset spare freshly paired with this bridge. This commissioning flow is still being qualified for Anyplace hardware.</p>
    <p>The bridge will attempt one assignment to a dedicated, empty HA event target and then listen for matching input. The original assignment is unknown: it cannot be read, backed up, cleared or automatically restored.</p>
    <p>Keep the dimmer nearby. After starting, wait for the wake prompt, then press the small Wake-Up button on the bottom once. When asked, use On, Off or the slider so the bridge can observe its target.</p>
    <p>No write is retried automatically. If the result is uncertain, review Operations before another change. A matching command does not prove Home Assistant receipt or that direct light control is isolated.</p>
    <div class="row"><button class="btn primary" onclick="confirmAnyplaceSetup()">Start experimental setup</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
}
function confirmAnyplaceSetup() {
  const r = accessoryReview;
  if (!r || r.model !== 81 || dev(r.id)?.product_type !== 81 || !$('#modalRoot').innerHTML ||
      r.action !== 'setup_ha' || r.instance !== S.status.instance || !accessoryAllowed(r.id, r.action)) {
    closeModal(); toast('Bridge status changed. Review Anyplace setup again.'); return;
  }
  closeModal(); accessoryReview = null;
  accessoryRequest(r.id, r.action, '/api/controller/setup', {target:r.id});
}
function anyplacePhaseText(id) {
  const r = accessoryRequests.get(id), job = S.status.jobs?.[id];
  if (!r?.pending || r.model !== 81 || r.action !== 'setup_ha' || r.instance !== S.status.instance ||
      !r.job || job?.id !== r.job || job.state !== 'running') return '';
  return {waiting_for_wake:'Press the small Wake-Up button on the bottom once now. Wait for the next prompt.',
    observing:'Wake received. Use On, Off or the slider now so the bridge can observe the assigned target.'}[S.status.anyplace_progress?.[id]] || '';
}
function anyplaceDetails(d, v) {
  if (v.control_configuration) return dimmerControlDetails(d, v);
  const id = d.avion_id, r = accessoryRequests.get(id), configured = v.anyplace_ha?.state === 'configured';
  const phase = anyplacePhaseText(id), mqtt = S.status.mqtt_enabled === true && S.status.mqtt_ready === true;
  const reason = v.actions?.setup_ha?.reason;
  return `<div class="sec"><h4>Anyplace dimmer control</h4>
    <p>Sharing publishes observed On, Off and brightness events to Home Assistant. It does not remove this dimmer’s direct control of its current target.</p>
    ${configured ? `<p><strong>Experimental HA target configured</strong></p>
      <p>Target <span class="mono">${esc(v.anyplace_ha.target)}</span> · ${v.anyplace_ha.observed_in_session ? 'Matching input observed recently.' : 'Press On, Off or move the slider to check input after setup or reconnect.'}</p>
      <p>Test direct light behavior with this controller’s Home Assistant automation disabled, then test its actions through Home Assistant. Stored assignment readback, Home Assistant receipt and physical isolation are not verified here.</p>`
      : v.anyplace_ha?.state === 'needs_review' ? '<p>Previous Anyplace setup needs review. Do not repeat setup. If an assignment was attempted, recovery can observe input without resending it.</p><p>' + opsReviewButton(v.anyplace_ha?.transaction) + '</p>'
      : `<button class="btn primary" onclick="openAnyplaceSetup(${id})" ${accessoryAllowed(id,'setup_ha') ? '' : 'disabled'}>Set up experimental HA control</button>
      <p class="hint">${esc(!mqtt ? 'Connect MQTT in Settings before HA setup.' : reason === 'model_setup_disabled'
        ? 'Experimental Anyplace HA setup is not enabled on this bridge.' : ACCESSORY_REASONS[reason] || 'For a factory-reset spare freshly paired with this bridge. Physical qualification is still required.')}</p>`}
    ${phase ? `<p role="status" aria-live="polite"><strong>${esc(phase)}</strong></p>` : ''}
    ${!configured ? `<details><summary>Direct group control</summary><p>Changes the dimmer’s direct lighting group. This is separately gated and still requires physical testing.</p>
      <button class="btn" onclick="openAccessoryNative(${d.avion_id})" ${accessoryAllowed(d.avion_id,'assign_native_target') ? '' : 'disabled'}>Choose direct group</button>
      <p class="hint">${esc(ACCESSORY_REASONS[v.actions?.assign_native_target?.reason] || 'Observe the current target, then review the group change.')}</p></details>` : ''}
    ${r ? `<div role="status" aria-live="polite"><p>${esc(r.message)}</p>${r.review ? opsReviewButton(r.transaction) : ''}</div>` : ''}
    <p class="hint">The original assignment is unknown. Stored assignment reads, clear and automatic restoration are unavailable. Accepting a direct target change or recovering an uncertain setup is done in Operations.</p></div>`;
}

/* ---------- battery keypad setup ---------- */
const batterySetups = new Map();
let batteryReview = null;
const BATTERY_PHASES = {
  cooldown:'Wait before the next wake. The cooldown can take 20 seconds; do not keep pressing buttons.',
  waiting_for_wake:'Press the bottom Power/Off button once now to wake the battery keypad.',
  settling:'Wake received. Let the keypad settle; do not press another button yet.',
  reading:'Reading the keypad. Wait for the next wake prompt.',
  verified:'Readback received. Waiting for the operation to finish.',
  needs_review:'This operation needs review. Wait for its result before another change.'
};
function batterySetupAllowed(id) {
  const v = keypadView(id), r = batterySetups.get(id);
  return dev(id)?.product_type === 126 && managementFresh() && S.status.mesh_ready === true &&
    S.status.mqtt_enabled === true && S.status.mqtt_ready === true && !S.status.pending_transactions &&
    !S.radio.label && !S.radio.queue.length && !groupBusy && !r?.review && !r?.completed &&
    ![...batterySetups.values(), ...accessoryRequests.values()].some(request => request.pending) &&
    !Object.values(S.status.jobs || {}).some(j => j.state === 'running') &&
    v?.actions?.setup_ha?.enabled === true &&
    (!v.assignment_read_back || v.slots.some(row => row.status === 'unused'));
}
function settleBatterySetups() {
  for (const [id, r] of batterySetups) {
    if (!r.pending || !r.job) continue;
    if (r.instance !== S.status.instance) {
      Object.assign(r, {pending:false, review:true, message:'The bridge restarted during battery setup. Review the recorded operation in Operations; nothing will be repeated automatically.'});
      continue;
    }
    const job = S.status.jobs?.[id], v = keypadView(id);
    if (!job || job.id !== r.job || job.state === 'running') continue;
    const result = job.result;
    const verified = job.state === 'completed' && v?.assignment_read_back === true &&
      (result?.setup_complete === true && v.keypad_ha_setup?.transaction === result.transaction &&
        v.keypad_ha_setup.revision === v.revision && v.keypad_ha_setup.slots.length > 0 ||
       result?.changed === false && result.table?.revision === v.revision);
    if (verified) {
      const owned = v.slots.filter(row => row.ha_owned).length;
      Object.assign(r, {pending:false, completed:true, review:result.ready !== true,
        message:(result.changed === false ? 'No unused buttons needed setup. Existing assignments were preserved.'
          : `Unused buttons were configured and read back; existing assignments were preserved. ${owned} button${owned === 1 ? ' has' : 's have'} verified HA targets.`)
          + (result.ready === true ? ' Choose each button’s action in Home Assistant.'
             : ' HA event publication is not ready. Read the buttons again to review the preserved assignments.')});
    } else if (job.state === 'not_started' && operationNotStarted(job)) {
      Object.assign(r, {pending:false, review:false, message:'Battery setup did not start. '+(job.message || '')});
    } else {
      Object.assign(r, {pending:false, review:true, message:'Battery setup is not confirmed. Review Operations before another change. No write will be repeated automatically.'});
    }
  }
}
function openBatterySetup(id) {
  if (!batterySetupAllowed(id)) return;
  batteryReview = {id, instance:S.status.instance};
  modal(`<h3>Set up unused buttons for Home Assistant?</h3>
    <p>The bridge will read the complete keypad table and fill only unused buttons with dedicated HA targets. Existing direct-control and unknown assignments are preserved.</p>
    <p>Close the HALO app. Keep the keypad nearby and follow each wake prompt below. Press the bottom Power/Off button only when asked; a 20-second cooldown can occur between wakes. Several wakes may be needed for each button.</p>
    <p>Every write is attempted once and checked by reading the table back. If setup stops, review the recorded operation before continuing.</p>
    <div class="row"><button class="btn primary" onclick="confirmBatterySetup()">Start battery setup</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
}
async function confirmBatterySetup() {
  const review = batteryReview;
  if (!review || !$('#modalRoot').innerHTML || review.instance !== S.status.instance || !batterySetupAllowed(review.id)) {
    closeModal(); toast('Bridge status changed. Review battery setup again.'); return;
  }
  closeModal(); batteryReview = null;
  const r = {pending:true, review:false, job:null, instance:S.status.instance, message:'Starting battery setup… Do not repeat this request.'};
  batterySetups.set(review.id, r); renderSoon();
  try {
    const result = await api('/api/controller/setup', {target:review.id});
    if (result.accepted !== true || !result.job_id || r.instance !== S.status.instance) throw Error('Setup acceptance was not confirmed.');
    r.job = result.job_id; r.message = 'Battery setup accepted. Follow the wake prompts; existing assignments will be preserved.';
  } catch (e) {
    Object.assign(r, {pending:false, review:!operationNotStarted(e.details),
      message:operationNotStarted(e.details) ? 'Battery setup did not start. '+e.message
        : 'Battery setup outcome is not confirmed. Review Operations before another change. '+e.message});
  }
  await refreshSnapshot(); settleBatterySetups(); renderSoon();
}
function batteryPhaseText(id) {
  const job = S.status.jobs?.[id];
  if (job && Object.prototype.hasOwnProperty.call(job, 'progress')) {
    const p = job.progress;
    if (job.state !== 'running' || !p || dev(id)?.product_type !== 126 ||
        p.job_id !== job.id || p.target !== id || p.product_id !== 126 ||
        typeof p.instance !== 'string' || p.instance !== S.status.instance ||
        !['keypad_read','keypad_save','keypad_setup','keypad_recovery'].includes(p.operation)) return '';
    return BATTERY_PHASES[p.phase] || '';
  }
  const requests = [controllerReads.get(id), keypadSaves.get(id), batterySetups.get(id)];
  if (!job || job.state !== 'running' || !requests.some(r => r?.job === job.id && r.instance === S.status.instance)) return '';
  return BATTERY_PHASES[S.status.keypad_progress?.[id]] || '';
}
function batteryDetails(d, v) {
  const r = batterySetups.get(d.avion_id), phase = batteryPhaseText(d.avion_id);
  const mqtt = S.status.mqtt_enabled === true && S.status.mqtt_ready === true;
  const noUnused = v.assignment_read_back && !v.slots.some(row => row.status === 'unused');
  const reason = !mqtt ? 'Connect MQTT in Settings before HA setup.' : noUnused
    ? 'There are no unused buttons. Existing assignments are preserved; setting up unused buttons will not convert direct controls to HA-only.'
    : v.actions?.setup_ha?.reason === 'model_setup_disabled' ? 'Automatic setup of unused buttons is disabled. ' + (v.editor_available === true
      ? 'Individual button editing is enabled: read the buttons, then choose Home Assistant for each editable button.'
      : 'Individual button editing is separately controlled; after reading, use Home Assistant when the editor is enabled.')
    : ACCESSORY_REASONS[v.actions?.setup_ha?.reason] || 'Setup fills only unused buttons. Battery commissioning still needs physical testing.';
  return `<div class="sec"><h4>Battery keypad control</h4>
    <p>Close the HALO app. Each read round or programming stage can require another wake. Press the bottom Power/Off button only when prompted.</p>
    ${!v.assignment_read_back && v.cached_mapping?.status === 'cached' ? `<p><strong>Using saved button mappings.</strong> HA buttons ${v.cached_mapping.slots.map(row => row.slot).join(', ')} can publish named events after a restart. These assignments have not been read in this connection.</p><p class="hint">Wake and read before editing. A button swap made outside this bridge may not be detected from presses alone. Inventory changes disable saved mappings until another successful read.</p>` : !v.assignment_read_back ? '<p>No usable saved button mapping. Wake and read assignments to enable named button events. Verified, bridge-owned HA button mappings can then be saved for future restarts.</p>' : ''}
    ${phase ? `<p role="status" aria-live="polite"><strong>${esc(phase)}</strong></p>` : ''}
    <button class="btn primary" onclick="openBatterySetup(${d.avion_id})" ${batterySetupAllowed(d.avion_id) ? '' : 'disabled'}>Set up unused buttons for HA</button>
    <p class="hint">${esc(reason)}</p>
    ${r ? `<div role="status" aria-live="polite"><p>${esc(r.message)}</p>${r.review ? opsReviewButton(r.transaction) : ''}</div>` : ''}
    <p class="hint">Read the buttons to choose Direct group, Home Assistant or Unused for each editable button. HA button actions require MQTT and a Home Assistant automation.</p></div>`;
}

/* ---------- controller details ---------- */
const controllerReads = new Map();
function controllerReadAllowed(id) {
  return managementFresh() && !S.radio.label && !S.radio.queue.length && !groupBusy &&
    S.controllerViews.find(v => v.device_id === id)?.actions?.read_assignments?.enabled === true &&
    (!controllerReads.has(id) || controllerReads.get(id).retrySafe === true);
}
async function readController(id) {
  if (!controllerReadAllowed(id)) return;
  const read = {message:'Reading assignments…', job:null, instance:S.status.instance};
  controllerReads.set(id, read); renderSoon();
  try {
    const result = await api('/api/keypad/read', {target:id});
    if (result.accepted !== true || !result.job_id || read.instance !== S.status.instance) throw Error('Read acceptance was not confirmed.');
    read.message = 'Read accepted. Waiting for both table reads.'; read.job = result.job_id;
  } catch (e) {
    controllerReads.set(id, operationNotStarted(e.details)
      ? {message:'Read not started. '+e.message, job:null, retrySafe:true}
      : {message:'Read outcome not confirmed. Review it in Operations. '+e.message, job:null});
  }
  await refreshSnapshot(); renderSoon();
}
function controllerDetails(d) {
  const v = S.controllerViews.find(v => v.device_id === d.avion_id);
  if (!v) return '<p>Waiting for controller status.</p>';
  const read = controllerReads.get(d.avion_id), job = S.status?.jobs?.[d.avion_id];
  if (read?.job && read.instance !== S.status.instance) {
    read.message = 'The bridge restarted during the read. Read the buttons again when the bridge is ready.';
    read.retrySafe = true;
  }
  if (read?.job && read.instance === S.status.instance && job?.id === read.job && job.state !== 'running') {
    if (job.state === 'completed' && v.assignment_read_back) {
      controllerReads.delete(d.avion_id);
      if (v.actions?.edit_assignment?.enabled === true && keypadSaves.get(d.avion_id)?.review) {
        keypadSaves.delete(d.avion_id); keypadDrafts.delete(d.avion_id);
      }
    }
    else if (job.state === 'not_started' && operationNotStarted(job)) {
      read.message = 'Read not started. '+job.message; read.retrySafe = true;
    }
    else read.message = 'Read needs review. Check Operations for details.';
  }
  const ha = {disabled:'MQTT is not enabled. Set it up in Settings to send events to Home Assistant.',disconnected:'MQTT is disconnected. Home Assistant button control is unavailable until it reconnects.',not_exposed:'This controller is not shared. Choose Add to Home Assistant below to publish its button events.',publishing_enabled:'MQTT is connected and this controller is shared. Event publication is enabled; actual Home Assistant receipt is not verified here.'};
  const event = v.last_event;
  const inputTime = Number.isFinite(event?.age_ms) && event.age_ms >= 0 && snapshotAt
    ? new Date(snapshotAt - event.age_ms).toLocaleTimeString() : '';
  return `<p>${esc(ha[v.ha_status] || 'Home Assistant status unknown.')}</p>
    ${d.product_type === 91 ? accessoryDetails(d, v) : ''}
    ${d.product_type === 81 ? anyplaceDetails(d, v) : ''}
    ${d.product_type === 126 ? batteryDetails(d, v) : ''}
    <p>${event ? `Last observed input${inputTime ? ' at '+esc(inputTime) : ''}: ${esc(event.event_type)}${event.button ? ' · button '+esc(event.button) : ''}${event.brightness !== undefined ? ' · level '+esc(event.brightness)+'/255' : ''}${event.target ? ' · target '+esc(event.target) : ''}. This is observed behavior, not stored assignment proof.` : 'No controller input observed in this connection yet.'}</p>
    ${[126,127].includes(d.product_type) ? `<p>${v.assignment_read_back ? 'Button assignments verified in this connection.' : 'Stored assignments have not been read back.'}</p>` : ''}
    ${keypadEditorVisible(d.avion_id) ? keypadEditorHtml(d, v) : (v.slots || []).map(row => `<p>Button ${esc(row.slot)}: ${row.status === 'unused' ? 'Unused' : `${esc(row.status.replaceAll('_',' '))} · ${esc(gName(row.target))} · preset ${esc(row.preset)}/255`}</p>`).join('')}
    ${keypadEditorNotice(d.avion_id)}
    ${keypadSaveStatus(d.avion_id)}
    ${[126,127].includes(d.product_type) ? `<button class="btn" onclick="readController(${d.avion_id})" ${controllerReadAllowed(d.avion_id) ? '' : 'disabled'}>Read button assignments</button>` : ''}
    ${controllerReads.has(d.avion_id) ? `<p role="status">${esc(controllerReads.get(d.avion_id).message)}</p>` : ''}
    ${accessoryInputHtml(d, v)}
    ${(v.recovery || []).length ? `<p>This controller has an unfinished operation. Nothing is retried automatically. ${opsReviewButton(v.recovery.length === 1 ? v.recovery[0].transaction : null)}</p>` : ''}`;
}

function ctrlDrawer(d) {
  return `<div class="drawer-head"><span style="width:22px;color:var(--muted)">${[126,127].includes(d.product_type) ? I.keypad : I.dimmer}</span><div class="t"><h3>${esc(d.name)}</h3><div class="small muted">${esc(typeName(d))}${roomOf(d) ? ' · ' + esc(roomOf(d)) : ''}</div></div><button class="icon-btn" aria-label="Close" onclick="closeDrawer()">${I.close}</button></div>
  <div class="drawer-body">
    <div class="sec"><h4>What it controls</h4><div class="callout info">${I.info}<div>${controllerDetails(d)}</div></div></div>
    <div class="sec"><h4>Name and room</h4>${nameField(d)}${roomField(d)}</div>
    ${haSharingHtml('device', d)}
    <div class="sec"><h4>Details</h4><dl class="kv"><dt>Mesh ID</dt><dd class="mono">#${d.avion_id}</dd><dt>Type</dt><dd>${esc(typeName(d))} <span class="muted mono">(${d.product_type})</span></dd><dt>Type source</dt><dd>${typeSourceText(d)}</dd></dl></div>
    ${deviceToolsHtml(d)}
  </div>`;
}
function groupDrawer(g) {
  const ms = members(g.group_id).sort(byName); const byRoom = {};
  ms.forEach(d => (byRoom[roomOf(d) || NO_ROOM] = byRoom[roomOf(d) || NO_ROOM] || []).push(d));
  const ct = ms.length && ms.every(d => d.has_color_temp);
  const reported = ms.filter(d => d.brightness_fresh && Number.isInteger(d.brightness) && d.brightness >= 0 && d.brightness <= 255);
  // Match the bridge's group brightness: highest observed member, including OFF.
  const brightness = reported.length ? pct(Math.max(...reported.map(d => d.brightness))) : null;
  const brightnessNote = !reported.length ? 'Waiting for light readings.' : [
    reported.length < ms.length ? `Based on readings from ${reported.length} of ${ms.length} lights.` : '',
    new Set(reported.map(d => d.brightness)).size > 1 ? 'Lights report different levels; the slider shows the highest.' : ''
  ].filter(Boolean).join(' ');
  const color = ct && ms.every(d => d.color_temp_fresh && d.color_temp === ms[0].color_temp) ? ms[0].color_temp : null;
  return `<div class="drawer-head"><span style="width:22px;color:var(--muted)">${I.layers}</span><div class="t"><h3>${esc(g.name)}</h3><div class="small muted">${ms.length} light${ms.length === 1 ? '' : 's'} · group <span class="mono">#${g.group_id}</span></div></div><button class="icon-btn" aria-label="Close" onclick="closeDrawer()">${I.close}</button></div>
  <div class="drawer-body">
    ${ms.length && feat('control') && !g.protected ? `<div class="sec"><div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">${groupPowerSwitch(g.group_id, 'Power')}<b>${powerCounts(ms)}</b></div>
      ${ms.every(canDim) ? `<div class="slider-lab"><label for="gBri">Brightness</label><span id="gBriV" class="mono">${brightness === null ? '–' : brightness + '%'}</span></div>
      <input id="gBri" type="range" min="0" max="100" value="${brightness ?? 0}" oninput="$('#gBriV').textContent=this.value+'%'" onchange="setBrightness(${g.group_id},this.value)">
      ${brightnessNote ? `<p class="hint">${brightnessNote}</p>` : ''}` : ''}
      ${ct ? kelvinControls(g.group_id, color) : ''}<p class="hint">One command goes to the whole group.</p></div>` : ''}
    ${haSharingHtml('group', g)}
    <div class="sec"><h4>Lights in this group</h4>${ms.length ? Object.entries(byRoom).map(([r, ds]) => `<div class="small muted" style="margin:8px 0 2px;font-weight:600">${esc(r)}</div>
      <div class="checklist">${ds.map(d => `<label style="cursor:pointer" onclick="openDrawer('device',${d.avion_id})">${glow(d, false)}${esc(d.name)}<span class="mono muted small" style="margin-left:auto">#${d.avion_id}</span></label>`).join('')}</div>`).join('') : '<p class="hint" style="margin:0">This group has no lights.</p>'}
      ${!g.protected && g.group_id !== 24566 ? `<div class="row"><button class="btn" onclick="openGroupMembers(${g.group_id},true)" ${groupWritable(g.group_id) ? '' : 'disabled'}>Add lights</button><button class="btn" onclick="openGroupMembers(${g.group_id},false)" ${groupWritable(g.group_id) && ms.length ? '' : 'disabled'}>Remove lights</button></div>` : ''}
      ${S.groupWorkflow?.programming_enabled ? '' : '<p class="hint">Membership programming and readback are disabled on this bridge. Creating a name does not program a group into the lights.</p>'}</div>${groupResultHtml(g.group_id)}
    ${!g.protected && g.group_id !== 24566 && feat('group_metadata') ? `<div class="sec"><h4>Remove group</h4><p class="hint">${g.members.length ? 'Remove all lights from this group above, and wait for membership verification before removing the empty group.' : 'Remove this empty group from the bridge. Lights and controllers stay paired.'}</p><button class="btn danger" onclick="confirmRemoveGroup(${g.group_id})" ${groupRemovable(g.group_id) ? '' : 'disabled'}>Remove group…</button></div>` : ''}
  </div>`;
}

/* ---------- reviewed group management ---------- */
const groupOutcomes = new Map();
let groupBusy = false, groupDraft = null, groupCreation = null, snapshotAt = 0;
function managementFresh() {
  return S.synced && Date.now() - snapshotAt < 15000 && S.status?.preview_only !== true;
}
function groupProgrammingReady() {
  return managementFresh() && !groupBusy && !S.radio.label && !S.radio.queue.length && S.status?.mesh_ready === true &&
    !S.status.pending_transactions && !Object.values(S.status.jobs || {}).some(j => j.state === 'running') &&
    S.groupWorkflow?.programming_enabled === true;
}
function groupWritable(id) {
  const g = grp(id);
  return groupProgrammingReady() && g && !g.protected && id !== 24566 && !groupOutcomes.get(id)?.blocked;
}
function roomGroupPlan(room) {
  const devices = roomLights(room).slice().sort((a,b)=>a.avion_id-b.avion_id);
  const allowed = new Set((S.groupWorkflow?.lights || []).filter(d=>d.eligible).map(d=>d.target));
  return {room, ids:devices.map(d=>d.avion_id), targets:devices.filter(d=>allowed.has(d.avion_id)).map(d=>d.avion_id),
    excluded:devices.filter(d=>!allowed.has(d.avion_id)).map(d=>d.name)};
}
function roomGroupPlanCurrent(plan) {
  if (!plan) return true;
  const now = roomGroupPlan(plan.room);
  return JSON.stringify(now.ids) === JSON.stringify(plan.ids) && JSON.stringify(now.targets) === JSON.stringify(plan.targets);
}

function groupResultHtml(id) {
  const r = groupOutcomes.get(id); if (!r) return '';
  const labels = {verified:'Membership verified',not_applied:'Requested membership not observed',needs_readback:'Needs readback',
    not_verified:'Not verified',not_attempted:'Not attempted',verified_publication_pending:'Membership verified; publication pending'};
  return `<div class="sec" role="status"><p>${esc(r.message)}</p>${(r.rows || []).map(row =>
    `<p>${esc(row.name || '#' + row.target)}: ${esc(labels[row.status] || row.status)}${r.blocked && row.transaction && row.status !== 'verified' ? ' ' + opsReviewButton(row.transaction) : ''}</p>`).join('')}
    ${r.blocked && !(r.rows || []).some(row => row.transaction && row.status !== 'verified') ? opsReviewButton(r.transaction) : ''}</div>`;
}
let groupRemovalReview = null;
function groupRemovable(id) {
  const g = grp(id);
  return feat('group_metadata') && managementFresh() && !groupBusy && !S.radio.label && !S.radio.queue.length &&
    !S.status.pending_transactions && !Object.values(S.status.jobs || {}).some(j => j.state === 'running') &&
    g && !g.protected && id !== 24566 && !g.members.length && !groupOutcomes.get(id)?.blocked;
}
function confirmRemoveGroup(id) {
  if (!groupRemovable(id)) return;
  groupRemovalReview = {id, revision:S.revision, instance:S.status.instance, attempted:false};
  modal(`<h3>Remove ${esc(grp(id).name)}?</h3><p>This removes the empty group from the bridge. No lights or controllers are reset, and no radio commands are sent.</p><p class="hint">Reassign any wall controls that still target this group. Its group number is kept reserved and will not be reused.</p><p id="groupFormStatus" role="status"></p><div class="row"><button class="btn" onclick="closeModal()">Cancel</button><button id="removeGroupSubmit" class="btn danger" onclick="removeEmptyGroup()">Remove group</button></div>`);
}
async function removeEmptyGroup() {
  const review = groupRemovalReview;
  if (!review || review.attempted || !groupRemovable(review.id)) return;
  if (review.revision !== S.revision || review.instance !== S.status.instance)
    return toast('Status or inventory changed. Close this dialog and review the group again.');
  review.attempted = true; groupBusy = true; $('#removeGroupSubmit').disabled = true;
  let saved = false;
  try {
    const result = await api('/api/metadata', {operation:'forget_empty_group', revision:review.revision, target:review.id});
    saved = true; S.revision = result.revision;
    await refreshSnapshot(true);
    closeModal(); closeDrawer(); toast('Empty group removed. Lights and controllers stay paired.');
  } catch (e) {
    const refused = !saved && [400,401,403,409,501].includes(e.status);
    const message = saved ? 'Group removed, but the view could not refresh. Reload to see the current groups.'
      : refused ? 'Group removal not started. ' + e.message
      : 'Removal outcome unknown. Reload the group list before another change. Nothing will be retried automatically.';
    if (!refused) groupOutcomes.set(review.id, {blocked:true, message});
    if ($('#groupFormStatus')) $('#groupFormStatus').textContent = message;
  } finally { groupBusy = false; }
}
function openCreateGroup(room = null) {
  if (!feat('group_metadata') || !managementFresh() || groupBusy) return;
  const source = typeof room === 'string' && room ? roomGroupPlan(room) : null;
  if (source && !source.ids.length) return toast('This room has no lights to group.');
  const existing = source && roomGroup(room);
  if (existing) { openDrawer('group', existing.group_id); return toast('A group already contains exactly this room’s lights.'); }
  const empty = source && lightingGroups().find(g => !g.members.length && g.name.trim().toLocaleLowerCase() === room.trim().toLocaleLowerCase());
  if (empty) {
    openDrawer('group', empty.group_id);
    return openGroupMembers(empty.group_id, true, source);
  }
  groupCreation = {source, revision:S.revision, instance:S.status.instance, attempted:false};
  const ready = !source || source.targets.length && groupProgrammingReady();
  modal(`<h3>${source ? 'Create group from room' : 'Create group'}</h3>
    <p>${source ? `Create a group for ${esc(room)}, then review its lights. ${source.targets.length} light${source.targets.length === 1 ? '' : 's'} will be preselected. Controllers are not added.` : 'Create a named empty group, then select the lights to add. Creating the name does not program any lights.'}</p>
    ${source?.excluded.length ? `<p class="hint">Not included because group setup is not supported for these lights yet: ${source.excluded.map(esc).join(', ')}.</p>` : ''}
    <label for="newGroupName">Group name</label><input class="inp" id="newGroupName" maxlength="128" required value="${esc(source ? room : '')}" placeholder="e.g. Theatre" aria-describedby="groupFormStatus" enterkeyhint="done" oninput="this.setCustomValidity('');this.removeAttribute('aria-invalid')" onkeydown="if(event.key==='Enter'&&!event.isComposing){event.preventDefault();createNamedGroup()}">
    <p id="groupFormStatus" role="status">${source && !ready ? (source.targets.length ? 'Group programming is unavailable or busy. Try again when the bridge is ready.' : 'No lights in this room currently support group setup.') : ''}</p><div class="row"><button class="btn" onclick="closeModal()">Cancel</button>
    <button id="createGroupSubmit" class="btn primary" onclick="createNamedGroup()" ${ready ? '' : 'disabled'}>${source ? 'Create group and review lights' : 'Create group'}</button></div>`);
}
async function createNamedGroup() {
  const input = $('#newGroupName'), name = input?.value.trim(), review = groupCreation;
  if (!input || !review || review.attempted || !feat('group_metadata') || !managementFresh() || groupBusy) return;
  if (review.revision !== S.revision || review.instance !== S.status.instance || !roomGroupPlanCurrent(review.source))
    return toast('The room or inventory changed. Close this dialog and review it again.');
  if (review.source && (!review.source.targets.length || !groupProgrammingReady())) return;

  if (!validLabel(name,128)) {
    input.setCustomValidity('Enter a group name with 1–128 printable characters.'); input.setAttribute('aria-invalid','true');
    $('#groupFormStatus').textContent = input.validationMessage; input.reportValidity(); return;
  }
  input.setCustomValidity(''); input.removeAttribute('aria-invalid');
  review.attempted = true; groupBusy = true; $('#createGroupSubmit').disabled = true;
  let saved = false, created = null;
  try {
    const result = await api('/api/metadata', {operation:'create_group', revision:review.revision, name});
    saved = true; S.revision = result.revision;
    if ($('#groupFormStatus')) $('#groupFormStatus').textContent = 'Group created. Loading its lights…';
    // A failed refresh must not invite another creation request.
    await refreshSnapshot(true);
    closeModal(); go('groups'); openDrawer('group', result.group_id);
    created = result.group_id;
    toast(review.source ? 'Group created. Review the preselected lights, then choose Add selected.' : 'Empty group created. Select lights to program its membership.');
  } catch (e) {
    const node = $('#groupFormStatus');
    if (node) node.textContent = saved ? 'Group created, but the view could not refresh. Reload to see it; do not create it again.' :
      [400,401,403,409,501].includes(e.status) ? e.message : 'Creation outcome unknown. Reload the group list before creating another group.';
  } finally { groupBusy = false; }
  if (created !== null && review.source) {
    if (!roomGroupPlanCurrent(review.source)) return toast('Group created, but the room changed. Use Add lights to review its current lights.');
    openGroupMembers(created, true, review.source);
  }
}
function openGroupMembers(id, member, source = null) {
  if (!groupWritable(id)) return toast('Group programming is unavailable, busy, or needs fresh status.');
  if (!roomGroupPlanCurrent(source)) return toast('The room changed. Review its lights again.');
  const g = grp(id), choices = (S.groupWorkflow.lights || []).filter(d => d.eligible && (member ? !g.members.includes(d.target) : g.members.includes(d.target)));
  groupDraft = {id, member, revision:S.revision, instance:S.status.instance, source, selected:new Set(source ? source.targets.filter(t=>choices.some(d=>d.target === t)) : []), allowed:choices.map(d => d.target)};
  modal(`<h3>${member ? 'Add lights to' : 'Remove lights from'} ${esc(g.name)}</h3>
    ${source ? `<p>Lights from ${esc(source.room)} are preselected. Review the selection below.</p>` : ''}
    <p>Only selected lights will change. The bridge reads each light before and after programming and stops if a result is uncertain.</p>
    <div class="checklist">${choices.map(d => `<label><input type="checkbox" ${groupDraft.selected.has(d.target) ? 'checked' : ''} onchange="selectGroupMember(${d.target},this.checked)">${esc(d.name)}</label>`).join('') || '<p>No eligible lights.</p>'}</div>
    <p class="hint">RL4 lights and A19 bulbs support verified group setup. Select up to 32 lights per change.</p>
    <p id="groupFormStatus" role="status"></p><div class="row"><button class="btn" onclick="closeModal()">Cancel</button>
    <button id="applyGroupMembers" class="btn primary" ${groupDraft.selected.size && groupDraft.selected.size <= 32 ? '' : 'disabled'} onclick="applyGroupMembers()">${member ? 'Add' : 'Remove'} selected</button></div>`);
}
function selectGroupMember(id, checked) {
  if (!groupDraft || groupBusy || !groupDraft.allowed.includes(id)) return;
  checked ? groupDraft.selected.add(id) : groupDraft.selected.delete(id);
  $('#applyGroupMembers').disabled = !groupDraft.selected.size || groupDraft.selected.size > 32;
}
async function applyGroupMembers() {
  const draft = groupDraft; if (!draft) return;
  const targets = [...draft.selected];
  const valid = () => groupDraft === draft && roomGroupPlanCurrent(draft.source) && groupWritable(draft.id) && draft.revision === S.revision && draft.instance === S.status.instance &&
    targets.length > 0 && targets.length <= 32 && targets.every(t => draft.allowed.includes(t) && draft.selected.has(t)) && targets.length === draft.selected.size;
  if (!valid()) return toast('Status or inventory changed. Close this dialog and review the lights again.');
  if (!confirm(`${draft.member ? 'Add' : 'Remove'} ${targets.map(t => dev(t)?.name || t).join(', ')} ${draft.member ? 'to' : 'from'} ${gName(draft.id)}? This changes the lights’ stored group membership.`)) return;
  if (!valid()) return toast('Status changed during confirmation. Review the selection again.');
  groupBusy = true; $('#applyGroupMembers').disabled = true;
  groupOutcomes.set(draft.id, {blocked:true, message:'Programming selected lights. Do not repeat this request.'});
  try {
    // One submission, never queued/retried. The existing backend owns readback,
    // durable receipts, per-light outcomes and stopping on uncertainty.
    const result = await api('/api/rooms/membership', {targets, group:draft.id, member:draft.member});
    groupOutcomes.set(draft.id, {...result, blocked:result.physical_memberships_verified !== true});
  } catch (e) {
    const notStarted = operationNotStarted(e.details);
    groupOutcomes.set(draft.id, notStarted
      ? {blocked:false, message:'Group change not started. '+e.message+' Review the lights and submit again when ready.'}
      : e.code === 'pending_operation' && e.details?.operation_started === false
      ? {blocked:true, transaction:e.details?.transaction, message:'Group change not started. '+e.message}
      : {blocked:true, transaction:e.details?.transaction, message:'Membership was not confirmed. '+e.message+' Review Operations before another change. Nothing will be retried automatically.'});
  } finally {
    groupDraft = null; groupBusy = false;
  }
  const outcome = groupOutcomes.get(draft.id);
  try { await refreshSnapshot(true); }
  catch (e) { outcome.message += ' The view could not refresh; reload for current inventory.'; }
  closeModal(); openDrawer('group', draft.id); renderSoon();
}


/* ---------- modals ---------- */
let modalReturnFocus = null;
function activeDialog() { return $('#modalRoot .modal') || $('#drawerRoot .drawer'); }
function syncDialogLayers() {
  const modalOpen = !!$('#modalRoot .modal'), drawerOpen = !!S.drawer;
  $('.app').inert = modalOpen;
  for (const selector of ['#nav','#main','#busyRoot']) $(selector).inert = drawerOpen;
}
function focusDialog() { const dialog=activeDialog(); if (dialog) { dialog.tabIndex=-1; dialog.focus({preventScroll:true}); } }
function restoreDialogFocus(node) {
  if (node?.id && !node.isConnected) node = document.getElementById(node.id);
  if (node?.isConnected && !node.closest('[inert]') && node.tagName !== 'SELECT') node.focus({preventScroll:true});
  else if (activeDialog()) focusDialog();
  else $('#navItems button[aria-current="true"]')?.focus({preventScroll:true});
}
function modal(html) {
  if (!$('#modalRoot').innerHTML) modalReturnFocus = document.activeElement;
  activeNativeSelect = null;
  $('#modalRoot').innerHTML = `<div class="modal-wrap" onclick="if(event.target===this)closeModal()"><div class="modal" role="dialog" aria-modal="true" tabindex="-1">${html}</div></div>`;
  const title=$('#modalRoot h3'); if(title){title.id='modalTitle';$('#modalRoot .modal').setAttribute('aria-labelledby',title.id);}
  syncDialogLayers();focusDialog();
}
function closeModal() {
  if (activeNativeSelect?.closest('#modalRoot')) activeNativeSelect=null;
  $('#modalRoot').innerHTML = '';syncDialogLayers();restoreDialogFocus(modalReturnFocus);modalReturnFocus=null;
}
document.addEventListener('keydown', e => {
  const dialog=activeDialog();if(e.key!=='Tab'||!dialog)return;
  const items=[...dialog.querySelectorAll('button:not(:disabled),input:not(:disabled):not([type=hidden]),select:not(:disabled),textarea:not(:disabled),a[href],[tabindex]:not([tabindex="-1"])')].filter(el=>el.getClientRects().length&&!el.closest('[inert]'));
  const first=items[0],last=items.at(-1),current=document.activeElement;
  if(!first){e.preventDefault();focusDialog();return;}
  if(!dialog.contains(current)||current===dialog||e.shiftKey&&current===first||!e.shiftKey&&current===last){e.preventDefault();(e.shiftKey?last:first).focus();}
});
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !e.isComposing && !composingField?.isConnected && !activeNativeSelect?.isConnected) { if ($('#modalRoot').innerHTML) closeModal(); else if (S.drawer) closeDrawer(); } });

function openNamer() {
  const dl = dupNames(lights());
  modal(`<h3>Name lights</h3><p>Press <b>Flash</b> and look up: the light in that row blinks twice. Then type a name for where it is.</p>
  <div class="name-rows">${dl.map(d => `<div class="name-row"><button class="btn sm" onclick="flash(${d.avion_id})">${I.flash}Flash</button>
    <input class="inp" id="nm${d.avion_id}" type="text" maxlength="80" value="${esc(d.name)}" aria-label="Name for #${d.avion_id}"></div>`).join('')}</div>
  <div class="row"><button class="btn" onclick="closeModal()">Later</button><button class="btn primary" onclick="saveNames([${dl.map(d => d.avion_id)}])">Save names</button></div>`);
}
function saveNames(ids) {
  const entries = {};
  ids.forEach(id => { const v = ($('#nm' + id)?.value || '').trim(); if (v && v !== dev(id).name) entries[id] = {name: v}; });
  if (!Object.keys(entries).length) { closeModal(); toast('No names changed'); return; }
  saveLabels(entries, true).then(ok => { if (ok) { closeModal(); const n = Object.keys(entries).length; toast(`Renamed ${n} light${n === 1 ? '' : 's'}`); log('system', `Renamed ${n} light${n === 1 ? '' : 's'}`); } });
}
/* Start rooms from existing groups. Earlier choices win when a light is in two chosen groups. */
function openRoomSetup() {
  const gs = S.groups.filter(g => g.members.length).sort((a, b) => a.members.length - b.members.length);
  modal(`<h3>Sort into rooms</h3><p>Pick the groups that match a room. Each light goes to the first ticked group it belongs to, smallest group first. Lights already in a room keep it.</p>
  <div class="checklist" style="max-height:50vh;overflow:auto">${gs.map(g => `<label><input type="checkbox" data-rg="${g.group_id}" onchange="roomPreview()">${esc(g.name)}<span class="muted small" style="margin-left:auto">${g.members.length} lights</span></label>`).join('')}</div>
  <p id="roomPrev" class="hint"></p>
  <div class="row"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn primary" id="roomApply" disabled onclick="applyRoomSetup()">Create rooms</button></div>`);
}
function roomPlan() {
  const chosen = [...document.querySelectorAll('[data-rg]:checked')].map(b => grp(+b.dataset.rg)).sort((a, b) => a.members.length - b.members.length);
  const plan = {};
  chosen.forEach(g => g.members.forEach(id => { const d = dev(id); if (d && d.kind !== 'controller' && !roomOf(d) && !plan[id]) plan[id] = {room: g.name}; }));
  return plan;
}
function roomPreview() {
  const plan = roomPlan(), n = Object.keys(plan).length, left = lights().filter(d => !roomOf(d) && !plan[d.avion_id]).length;
  $('#roomPrev').textContent = n ? `Puts ${n} light${n === 1 ? '' : 's'} into ${new Set(Object.values(plan).map(p => p.room)).size} room(s). ${left} light${left === 1 ? '' : 's'} left without a room.` : '';
  $('#roomApply').disabled = !n;
}
function applyRoomSetup() {
  const plan = roomPlan();
  saveLabels(plan, true).then(ok => { if (ok) { closeModal(); toast('Rooms created. Controllers can be given a room from their details.'); log('system', `Put ${Object.keys(plan).length} lights into rooms`); } });
}
function allLights(on) {
  modal(`<h3>Turn all ${lights().length} lights ${on ? 'on' : 'off'}?</h3><p>This sends one command to every light on the mesh, in every room.</p>
  <div class="row"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn primary" onclick="closeModal();control(0,{brightness:${on ? 255 : 0}},'Turn all lights ${on ? 'on' : 'off'}').catch(()=>{})">Turn all ${on ? 'on' : 'off'}</button></div>`);
}

/* ---------- add devices ---------- */
/* The bridge decodes each unpaired device's type from its UUID broadcast. Only unknown types ask the owner.
   The UI retains recent exact identities; the bridge freshly observes each selected identity before pairing and refuses a type that contradicts detection. */
const STEPS = ['Get ready', 'Find devices', 'Pair', 'Name'];
function newWizard() { return {step:0, room:'', roomDrafts:{}, saving:false, searching:false, stop:false, rounds:0, lastRound:0, searchComplete:false, found:{}, claims:[], names:{}, error:''}; }
function wizardNaming() {
  return S.wiz.autoNaming ||= {enabled:false,prefix:'',customPrefix:false,start:'',generated:{},originals:{},manual:{},error:''};
}
const wizardLightClaims = () => S.wiz.claims.filter(c => c.st === 'done' && ![81,91,126,127].includes(c.type));
const wizardNameKey = name => String(name || '').trim().normalize('NFKC').toLocaleLowerCase();
function wizardNamePrefix(id = null) {
  const a = wizardNaming();
  return (a.customPrefix ? a.prefix : id === null ? S.wiz.room : wizardRoomDraft(id).room).trim().replace(/\s+/gu,' ');
}
function wizardNextNumber(prefix, occupied) {
  let last = 0; const stem = wizardNameKey(prefix) + ' ';
  for (const name of occupied) {
    const tail = name.startsWith(stem) ? name.slice(stem.length) : '';
    if (/^[0-9]+$/.test(tail) && Number.isSafeInteger(+tail)) last = Math.max(last,+tail);
  }
  return last + 1;
}
function wizardPairingNames(selected) {
  const names = new Map(), a = wizardNaming();
  if (!feat('labels') || !a.enabled || !selected.some(f => ![81,91,126,127].includes(rowType(f)))) return names;
  const prefix = wizardNamePrefix();
  if (!prefix || [...prefix].length > 70 || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(prefix)) throw new Error('Choose a room or enter a name prefix with 1–70 printable characters.');
  if (a.start !== '' && (!/^[0-9]+$/.test(a.start) || +a.start < 1 || +a.start > 999999)) throw new Error('Enter a starting number from 1 to 999999, or leave it blank to continue existing numbers.');
  const occupied = new Set(S.devices.map(d => wizardNameKey(d.name)));
  let n = a.start === '' ? wizardNextNumber(prefix, occupied) : +a.start;
  for (const f of selected) {
    if ([81,91,126,127].includes(rowType(f))) continue;
    while (n <= 999999 && occupied.has(wizardNameKey(prefix+' '+n))) n++;
    if (n > 999999) throw new Error('No number is available with this prefix. Choose a different prefix or starting number.');
    const name = prefix+' '+n++; names.set(f.uuid, name); occupied.add(wizardNameKey(name));
  }
  return names;
}
function refreshWizardNames() {
  const w = S.wiz, a = wizardNaming(); a.error = '';
  // Recognise edits even if a draft was changed outside the input handler.
  for (const [id,name] of Object.entries(a.generated)) if (w.names[id] !== name) { a.manual[id] = true; delete a.generated[id]; }
  if (!a.enabled) {
    for (const id of Object.keys(a.generated)) {
      if (a.originals[id] === null) delete w.names[id]; else w.names[id] = a.originals[id];
    }
    a.generated = {}; a.originals = {}; return;
  }
  const candidates = wizardLightClaims().filter(c => !a.manual[c.id] && (!w.names[c.id] || Object.hasOwn(a.generated,c.id)));
  if (!candidates.length && w.step === 3) return;
  if (a.start !== '' && (!/^[0-9]+$/.test(a.start) || +a.start < 1 || +a.start > 999999)) {
    a.error = 'Enter a starting number from 1 to 999999, or leave it blank to continue existing numbers.'; return;
  }
  const ids = new Set(candidates.map(c => c.id));
  const occupied = new Set(S.devices.filter(d => !ids.has(d.avion_id)).map(d => wizardNameKey(w.names[d.avion_id] || d.name)));
  const next = new Map(), planned = {};
  // Validate the entire preview before changing any generated draft.
  for (const id of candidates.length ? candidates.map(c => c.id) : [null]) {
    const prefix = wizardNamePrefix(id);
    if (!prefix || [...prefix].length > 70 || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(prefix)) {
      a.error = 'Choose a room or enter a name prefix with 1–70 printable characters.'; return;
    }
    const key = wizardNameKey(prefix); let n = next.get(key) ?? (a.start === '' ? wizardNextNumber(prefix,occupied) : +a.start);
    while (n <= 999999 && occupied.has(wizardNameKey(prefix+' '+n))) n++;
    if (n > 999999) { a.error = 'No number is available with this prefix. Choose a different prefix or starting number.'; return; }
    if (id !== null) { planned[id] = prefix+' '+n; occupied.add(wizardNameKey(planned[id])); next.set(key,n+1); }
  }
  for (const [id,name] of Object.entries(planned)) {
    if (!Object.hasOwn(a.originals,id)) a.originals[id] = w.names[id] ?? null;
    a.generated[id] = name; w.names[id] = name;
  }
}
function wizardNamingHint() {
  const a = wizardNaming();
  if (a.error) return a.error;
  const names = Object.values(a.generated);
  if (names.length) return names.slice(0,3).join(', ') + (names.length > 3 ? '…' : '') + '. Review each name below before finishing.';
  const prefix = wizardNamePrefix(), n = a.start === '' ? wizardNextNumber(prefix,new Set(S.devices.map(d => wizardNameKey(d.name)))) : +a.start;
  return prefix ? `For example: ${prefix} ${n}, ${prefix} ${n+1}, ${prefix} ${n+2}. Review the names after pairing.` : 'Choose a room or enter a name prefix to preview the names.';
}
function wizardNamingHtml() {
  if (!feat('labels')) return '';
  const a = wizardNaming(), disabled = S.wiz.saving ? 'disabled' : '';
  return `<div class="wizard-naming"><label class="scene-select"><input id="wizardAutoName" type="checkbox" ${a.enabled ? 'checked' : ''} onchange="toggleWizardNames(this.checked)" ${disabled}><span><b>Auto-name and number lights</b><span class="small muted">Use the room name and a number. Individual edits are kept.</span></span></label>
    ${a.enabled ? `<div class="scene-values"><div class="field"><label for="wizardNamePrefix">Name prefix</label><input class="inp" id="wizardNamePrefix" maxlength="70" value="${esc(a.customPrefix ? a.prefix : S.wiz.room)}" placeholder="e.g. Theatre" oninput="changeWizardNaming('prefix',this.value)" ${disabled}></div><div class="field"><label for="wizardNameStart">Starting number</label><input class="inp" id="wizardNameStart" type="number" min="1" max="999999" step="1" inputmode="numeric" value="${esc(a.start)}" placeholder="Next available" oninput="changeWizardNaming('start',this.value)" ${disabled}></div></div>
    <p id="wizardNamingHint" class="${a.error ? 'err' : 'hint'}" role="status">${esc(wizardNamingHint())}</p><p class="hint">Existing names are skipped. Automatic names are saved as each light pairs. Controllers keep their own names. Changes on the final screen are saved when you press Finish.</p>` : ''}</div>`;
}
function paintWizardNames() {
  const a = wizardNaming(), hint = $('#wizardNamingHint');
  if (hint) { hint.textContent = wizardNamingHint(); hint.className = a.error ? 'err' : 'hint'; }
  for (const c of wizardLightClaims()) {
    const input = $('#wn'+c.id); if (input && input !== document.activeElement) input.value = S.wiz.names[c.id] || '';
  }
}
function toggleWizardNames(enabled) {
  if (!S.wiz || S.wiz.saving) return;
  wizardNaming().enabled = enabled; refreshWizardNames(); render();
}
function changeWizardNaming(field,value) {
  if (!S.wiz || S.wiz.saving || !['prefix','start'].includes(field)) return;
  const a = wizardNaming();
  if (field === 'prefix') { a.prefix = value; a.customPrefix = true; } else a.start = value;
  refreshWizardNames(); paintWizardNames();
}
function editWizardName(id,value) {
  if (!S.wiz || S.wiz.saving) return;
  S.wiz.names[id] = value; const a = wizardNaming(); a.manual[id] = true; delete a.generated[id];
  refreshWizardNames(); paintWizardNames();
}
function namePairedDevices() { S.wiz.step = 3; refreshWizardNames(); render(); }
function wizardNameConflict() {
  const a = wizardNaming(); if (!a.enabled) return false;
  return Object.entries(a.generated).some(([id,name]) => S.devices.some(d => String(d.avion_id) !== id && wizardNameKey(S.wiz.names[d.avion_id] || d.name) === wizardNameKey(name)));
}
function wizardRoomDraft(id) {
  const w = S.wiz;
  w.roomDrafts ||= {};
  if (id === null) return w.initialRoomDraft ||= {room:w.room || '', editing:false, name:'', error:''};
  return w.roomDrafts[id] ||= {room:roomOf(dev(id) || {}) || w.room || '', editing:false, name:'', error:''};
}
function wizardRoomField(id) {
  if (!feat('labels')) return '';
  const d = wizardRoomDraft(id), disabled = S.wiz.saving ? 'disabled' : '', fieldId = id === null ? 'wRoom' : 'wr'+id;
  const options = [...new Set([...rooms(), d.room, S.wiz.room, ...Object.values(S.wiz.roomDrafts).map(d => d.room)].filter(Boolean))].sort((a,b) => a.localeCompare(b));
  return `<div class="field" style="grid-column:1 / -1;margin:0"><label for="${fieldId}">Room</label>
    <select id="${fieldId}" class="inp" onchange="chooseWizardRoom(${id},this.value)" ${disabled}><option value="" ${!d.room ? 'selected' : ''}>No room</option>
    ${options.map(room => `<option value="${esc(JSON.stringify(room))}" ${room === d.room ? 'selected' : ''}>${esc(room)}</option>`).join('')}<option value="__new">New room…</option></select>
    ${d.editing ? `<div class="field" style="margin:8px 0 0"><label for="wrn${id}">New room name</label><input id="wrn${id}" class="inp" maxlength="40" value="${esc(d.name)}" oninput="wizardRoomDraft(${id}).name=this.value" onkeydown="if(event.key==='Enter'&&!event.isComposing){event.preventDefault();addWizardRoom(${id});}" ${disabled}>
      ${d.error ? `<p class="err" role="alert">${esc(d.error)}</p>` : ''}<div class="row" style="margin-top:8px"><button class="btn sm" onclick="addWizardRoom(${id})" ${disabled}>Add room</button><button class="btn sm" onclick="wizardRoomDraft(${id}).editing=false;render()" ${disabled}>Cancel</button></div></div>` : ''}
    <span class="hint">${id === null ? 'Applied to new devices after pairing.' : 'Saved when you finish.'} Assigning a room does not add lights to a group or change wall controls. For the best response from several lights together, create a group in Groups.</span></div>`;
}
function chooseWizardRoom(id, value) {
  if (!S.wiz || S.wiz.saving) return;
  const d = wizardRoomDraft(id); d.error = '';
  if (value === '__new') { d.editing = true; render(); $('#wrn'+id)?.focus(); return; }
  d.room = value ? JSON.parse(value) : ''; d.editing = false;
  if (id === null) S.wiz.room = d.room;
  if (wizardNaming().enabled) refreshWizardNames();
  render();
}
function addWizardRoom(id) {
  if (!S.wiz || S.wiz.saving) return;
  const d = wizardRoomDraft(id), name = d.name.trim().replace(/\s+/gu, ' ');
  if (!name || [...name].length > 40 || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(name)) {
    d.error = 'Enter a room name with 1–40 printable characters.'; render(); return;
  }
  d.room = name; d.name = name; d.editing = false; d.error = '';
  if (id === null) S.wiz.room = name;
  if (wizardNaming().enabled) refreshWizardNames();
  render();
}
function pairingClaimText(claim) {
  if (claim.type !== 81 || claim.st !== 'run' || !Number.isInteger(claim.uuid_hash) || claim.instance !== S.status?.instance) return claim.msg;
  const progress = Object.values(S.status.claim_progress || {}).filter(row => row?.uuid_hash === claim.uuid_hash);
  if (progress.length !== 1) return claim.msg;
  return {waiting_for_wake:'Keys exchanged. Press the small Wake-Up button on the bottom once now so the bridge can confirm pairing.',
    verifying:'Wake received. Checking the encrypted pairing reply; do not start another pairing.'}[progress[0].phase] || claim.msg;
}
function wizRows() { return Object.values(S.wiz.found).sort((a, b) => a.first - b.first); }
const pairable = () => S.status?.pairing?.models ??
  (S.status?.bootstrap?.needs_first_claim && !S.status.bootstrap.automatic ? [93] : S.caps.features.pairable_types || [93, 134]);
const pairingRemaining = () => S.status?.pairing?.attempts_remaining ?? Infinity;
function rowType(f) { return f.detected_product_type || f.manual || 0; }
function wizardDiscoveryEpoch() { return `${S.status?.instance || ''}:${S.status?.diagnostics?.session ?? ''}`; }
function rowSeenState(f) {
  const age = Date.now() - f.lastSeen;
  if (f.epoch !== wizardDiscoveryEpoch() || !Number.isFinite(age) || age < 0 || age > 300000) return 'old';
  return age <= 30000 ? 'fresh' : 'recent';
}
function rowReady(f) {
  return S.wiz.searchComplete === true && !f.ambiguous && rowSeenState(f) !== 'old' &&
    typeof f.uuid === 'string' && /^[a-f0-9]{32}$/.test(f.uuid) && /[1-9a-f]/.test(f.uuid) &&
    Number.isInteger(f.uuid_hash) && f.uuid_hash >= 0 && f.uuid_hash <= 0xffffffff &&
    !!rowType(f) && pairable().includes(rowType(f));
}
function viewAdd() {
  if (!feat('pair_lights')) return `<div class="view-head"><div><h2>Add devices</h2></div></div><p class="muted">Pairing isn't available on this bridge.</p>`;
  const w = S.wiz || (S.wiz = newWizard());
  const steps = `<ol class="steps">${STEPS.map((s, i) => `<li ${i === w.step ? 'aria-current="step"' : ''} class="${i < w.step ? 'done' : ''}"><i>${i < w.step ? '✓' : i + 1}</i>${s}</li>`).join('')}</ol>`;
  let b = '';
  if (w.step === 0) b = `<div class="cards">
    ${feat('pair_lights') === 'testing' ? `<div class="callout warn">${I.alert}<div><b>Still being tested on the Pi.</b> Pair a spare light first and check it responds before pairing the rest.</div></div>` : ''}
    <div class="card"><h3>Before you search</h3><ul style="margin:6px 0 0;padding-left:18px;line-height:1.8">
      <li>Power up the intended unpaired devices. Keep battery controllers awake during discovery and pairing.</li>
      <li>${S.status?.bootstrap?.needs_first_claim ? 'Keep the first powered, factory-reset light close to the bridge.' : "Keep one light that's already set up switched on nearby. The bridge talks through it."}</li>
      <li>Devices still paired to another mesh will not appear as unpaired. Reset only the device you intend to migrate, following its instructions.</li></ul>
      <p class="hint" style="margin-top:10px">You don't need to say what you're adding. The bridge reads each device's type from its broadcast and only asks when it can't tell. Available types: ${pairable().map(t => TYPE_NAMES[t]).join(', ')}. Controllers must be selected explicitly and configured separately after pairing.</p></div>
    ${feat('labels') ? `<div class="card"><h3>Where are they going?</h3><p>New devices are put in this room. You can change it later.</p>
      <div style="max-width:320px">${wizardRoomField(null)}</div>${wizardNamingHtml()}</div>` : ''}
    <div>${Number.isFinite(pairingRemaining()) ? `<p class="hint">${pairingRemaining()} pairing attempts available. Removing a device keeps its pairing history.</p>` : ''}<button class="btn primary" onclick="startSearch()" ${pairingRemaining() ? '' : 'disabled'}>Start searching</button></div></div>`;
  if (w.step === 1) {
    const rows = wizRows(), ready = rows.filter(f => f.sel && rowReady(f)).length;
    const fresh = rows.filter(f => rowSeenState(f) === 'fresh').length;
    const recent = rows.filter(f => rowSeenState(f) === 'recent').length;
    const old = rows.length - fresh - recent;
    const searchSummary = `${fresh} seen in the last 30 seconds.${recent ? ` ${recent} seen recently; checked again before pairing.` : ''}${old ? ` ${old} older result${old === 1 ? '' : 's'}; search again.` : ''}`;
    b = `<div class="toolbar"><div style="flex:1;min-width:0">${w.searching ? `<span class="pulse"></span> <b>Searching… round ${w.rounds} of 3</b> <span class="muted">${esc(searchSummary)} Each round listens for about 5 seconds.</span>` : `<b>${esc(searchSummary)}</b> <span class="muted">Search again if some are missing.</span>`}</div>
      ${w.searching ? `<button class="btn" onclick="S.wiz.stop=true;toast('Stopping after this round')">Stop after this round</button>` : `<button class="btn" onclick="startSearch(true)">Search again</button>`}</div>
    ${w.error ? `<p class="err">${esc(w.error)}</p>` : ''}
    <div class="found">${rows.map(foundRow).join('') || `<p class="muted">${w.searching ? 'Listening…' : 'Nothing found. Keep an existing light powered nearby. Wake battery controllers or power up the intended unpaired lights, then search again.'}</p>`}</div>
    <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:center"><button class="btn" ${w.searching ? 'disabled' : ''} onclick="wizRows().forEach(f=>{if(rowReady(f))f.sel=true});render()">Select all</button>
      <span class="muted small" style="flex:1">Only select devices you own. Nothing changes until you press Pair. Recently seen devices stay selectable for 5 minutes. The bridge checks each one again before pairing.</span>
      <button class="btn primary" ${ready && !w.searching ? '' : 'disabled'} onclick="confirmPair()">Pair ${ready} device${ready === 1 ? '' : 's'}</button></div>`;
  }
  if (w.step === 2) {
    const done = w.claims.filter(c => c.st === 'done').length, fail = w.claims.filter(c => c.st === 'fail').length, running = w.claims.some(c => c.st === 'run' || c.st === 'wait');
    b = `<p><b>${done} of ${w.claims.length} paired</b>${fail ? ` · <span class="err">${fail} need${fail === 1 ? 's' : ''} review</span>` : ''}. <span class="muted">Devices are paired one at a time and finished ones are never redone. Keep battery controllers awake.</span></p>
    <div class="panel">${w.claims.map((c, i) => `<div class="qrow"><span class="qicon ${c.st === 'done' ? 'ok' : c.st === 'fail' ? 'bad' : c.st === 'run' ? 'run' : ''}">${c.st === 'done' ? I.check : c.st === 'fail' ? '!' : c.st === 'run' ? '<span class="pulse"></span>' : i + 1}</span>
      <div style="min-width:0"><b style="font-weight:500">${esc(c.autoNamed ? c.name : TYPE_NAMES[c.type])}</b> <span class="mono muted small">${esc(c.hash_hex)}</span>${c.id ? ` <span class="mono small">→ #${c.id}</span>` : ''}
      <div class="small ${c.st === 'fail' ? 'err' : 'muted'}" role="status">${esc(pairingClaimText(c))}</div>${c.receipt ? opsReviewButton(c.receipt) : ''}</div><span></span></div>`).join('')}</div>
    ${fail ? `<p class="hint">A failed pairing isn't retried automatically. The bridge saved what happened. Review Operations before another attempt.</p>${w.claims.some(c => c.st === 'fail' && !c.receipt) ? opsReviewButton() : ''}` : ''}
    <div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end">${running ? `<button class="btn" onclick="S.wiz.stop=true;toast('Will stop after the current device')">Stop after this device</button>` : done ? `<button class="btn primary" onclick="namePairedDevices()">Name ${done} device${done === 1 ? '' : 's'}</button>` : `<button class="btn" onclick="S.wiz=newWizard();render()">Start over</button>`}</div>`;
  }
  if (w.step === 3) {
    const ok = w.claims.filter(c => c.st === 'done');
    b = `<p>Name each device. ${ok.some(c => ![81,91,126,127].includes(c.type)) ? 'Use Flash to identify a light. ' : ''}${ok.some(c => [81,91,126,127].includes(c.type)) ? 'Open a controller to review its input and setup options. ' : ''}You can rename devices later.</p>
    ${ok.some(c => ![81,91,126,127].includes(c.type)) ? `<div class="card" style="max-width:560px">${wizardNamingHtml()}</div>` : ''}
    <div class="name-rows" style="max-width:560px">${ok.map(c => `<div class="name-row card">${[81,91,126,127].includes(c.type) ? `<button class="btn sm" onclick="openDrawer('device',${c.id})">Open controller</button>` : `<button class="btn sm" onclick="flash(${c.id})">${I.flash}Flash</button>`}
      <input class="inp" type="text" maxlength="80" id="wn${c.id}" value="${esc(w.names[c.id] || '')}" placeholder="${esc(dev(c.id)?.name || c.name)}" aria-label="Name for device ${c.id}" oninput="editWizardName(${c.id},this.value)" ${w.saving ? 'disabled' : ''}>${wizardRoomField(c.id)}</div>`).join('')}</div>
    <div style="display:flex;gap:8px;margin-top:16px"><button class="btn primary" onclick="finishWizard()" ${w.saving ? 'disabled' : ''}>${w.saving ? 'Saving…' : 'Finish'}</button></div>`;
  }
  return `<div class="view-head"><div><h2>Add devices</h2><div class="sub">Pair lights and controllers to this bridge</div></div>${w.step > 0 && !w.saving && !w.searching && !w.claims.some(c => c.st === 'run') ? `<div class="actions"><button class="btn" onclick="S.wiz=newWizard();render()">Start over</button></div>` : ''}</div>${steps}${b}`;
}
function foundRow(f) {
  const seenState = rowSeenState(f), stale = seenState === 'old' || f.ambiguous, t = f.detected_product_type, can = rowReady(f), key = f.uuid_hash;
  const seenText = f.ambiguous ? 'Conflicting device identities were seen. Pairing is unavailable for this result.'
    : seenState === 'old' ? 'This result is over 5 minutes old or the bridge connection changed. Search again before pairing.'
    : seenState === 'recent' ? 'Seen recently — checked again before pairing' : 'Seen in the last 30 seconds — checked again before pairing';
  let typeHtml;
  if (t) typeHtml = `<span class="badge ok">Detected</span> ${pairable().includes(t) ? '' : `<span class="small muted">Pairing ${esc(article(TYPE_NAMES[t] || 'device of this type'))} isn't available on this bridge yet.</span>`}`;
  else typeHtml = `<span class="badge warn">Not recognised</span> <select class="inp" style="width:auto;display:inline-block;padding:2px 6px;margin-top:4px" aria-label="Choose type" onchange="S.wiz.found[${key}].manual=+this.value||0;S.wiz.found[${key}].sel=!!+this.value;render()">
      <option value="">Choose type…</option>${pairable().map(p => `<option value="${p}" ${f.manual === p ? 'selected' : ''}>${TYPE_NAMES[p]}</option>`).join('')}</select>${f.manual ? ' <span class="small muted">Chosen by you</span>' : ''}`;
  return `<div class="found-row ${stale ? 'stale' : ''}"><input type="checkbox" aria-label="Select ${esc(f.hash_hex)}" ${f.sel && can ? 'checked' : ''} ${can && !S.wiz.searching ? '' : 'disabled'} onchange="S.wiz.found[${key}].sel=this.checked;render()">
    <span style="min-width:0"><b style="font-weight:500">${esc(t ? (TYPE_NAMES[t] || 'Type ' + t) : f.manual ? TYPE_NAMES[f.manual] : 'Unknown type')}</b> <span class="mono muted">${esc(f.hash_hex)}</span><br>${typeHtml}
    <br><span class="small muted">${esc(seenText)} · <span class="mono">vendor ${f.vendor_code ?? '?'} / product ${f.product_code ?? '?'}</span></span></span>
    <span></span><span class="badge ${stale ? 'warn' : 'ok'}">${f.ambiguous ? 'Ambiguous' : stale ? 'Search again' : seenState === 'recent' ? 'Seen recently' : 'Nearby'}</span></div>`;
}
function scanRow(d) {
  if (!d || typeof d.uuid !== 'string' || !/^[a-f0-9]{32}$/.test(d.uuid) || !/[1-9a-f]/.test(d.uuid) ||
      !Number.isInteger(d.uuid_hash) || d.uuid_hash < 0 || d.uuid_hash > 0xffffffff) return null;
  const id = d.identity || {};
  return {uuid:d.uuid, uuid_hash:d.uuid_hash, hash_hex:(d.uuid_hash >>> 0).toString(16).padStart(8, '0'),
    vendor_code:id.vendor, product_code:id.product, detected_product_type:id.candidate_model || 0};
}
async function startSearch(again) {
  const w = S.wiz; if (w.searching) return;
  if (w.step === 0 && w.initialRoomDraft?.editing) { toast('Add or cancel the new room before searching.'); return; }
  w.step = 1; w.searching = true; w.stop = false; w.error = ''; w.rounds = 0; if (!again) w.found = {};
  render();
  for (let round = 1; round <= 3 && !w.stop; round++) {
    w.rounds = round; renderSoon();
    try {
      const epoch = wizardDiscoveryEpoch();
      const res = await radio(`Searching for unpaired devices (round ${round})`, () => api('/api/discover', {}, {retryBusy:true}));
      if (S.wiz !== w || epoch !== wizardDiscoveryEpoch()) { w.error = 'The bridge connection changed during discovery. Search again.'; w.searchComplete = false; break; }
      w.lastRound++; w.searchComplete = res.truncated !== true;
      if (res.truncated) w.error = 'Too many devices answered at once, so the list may be incomplete. Pairing waits for a complete search.';
      const now = Date.now(), history = Array.isArray(res.candidates) ? res.candidates : [];
      (res.devices || []).map(scanRow).filter(Boolean).forEach(d => {
        const cached = history.find(row => row.uuid === d.uuid), age = cached?.age_ms ?? 0;
        // Only this round's observations renew recency. Cumulative history never does.
        if (cached?.fresh === false || !Number.isFinite(age) || age < 0) return;
        const f = w.found[d.uuid_hash] || (w.found[d.uuid_hash] = {first:now, seen:0, sel:false, manual:0});
        if (f.uuid && f.uuid !== d.uuid) { f.ambiguous = true; f.sel = false; return; }
        if (f.epoch && f.epoch !== epoch) { f.sel = false; f.manual = 0; f.seen = 0; }
        Object.assign(f, d); f.seen++; f.lastRound = w.lastRound; f.lastSeen = now - age; f.epoch = epoch;
        if (cached?.ambiguous_hash === true) f.ambiguous = true;
        if (f.seen === 1 && !f.ambiguous && [93,134].includes(d.detected_product_type) && pairable().includes(d.detected_product_type)) f.sel = true;
      });
      for (const candidate of history) {
        const f = w.found[candidate.uuid_hash];
        if (f && (candidate.ambiguous_hash === true || candidate.uuid !== f.uuid)) { f.ambiguous = true; f.sel = false; }
      }
      log('system', `Search round ${round}`, `${(res.devices || []).length} unpaired device${(res.devices || []).length === 1 ? '' : 's'} heard`);
    } catch (e) { w.error = e.message; w.searchComplete = false; break; }
    // Leave a short gap so light control and the state poll can use Bluetooth between rounds.
    if (round < 3 && !w.stop) await sleep(1200);
  }
  w.searching = false; w.searchedAt = Date.now(); render();
}
function confirmPair() {
  const sel = wizRows().filter(f => f.sel && rowReady(f));
  if (sel.length > pairingRemaining()) { toast(`Only ${pairingRemaining()} pairing attempts are available.`); return; }
  if (!sel.length) { toast('No recently seen devices are selected. Search again first.'); return; }
  try { wizardPairingNames(sel); } catch (e) { toast(e.message); return; }
  const counts = {}; sel.forEach(f => counts[TYPE_NAMES[rowType(f)]] = (counts[TYPE_NAMES[rowType(f)]] || 0) + 1);
  const manual = sel.filter(f => !f.detected_product_type).length;
  modal(`<h3>Pair ${sel.length} device${sel.length === 1 ? '' : 's'}?</h3>
  <p>${Object.entries(counts).map(([k, n]) => `${n} × ${esc(k)}`).join(', ')}${manual ? ` (${manual} chosen by you)` : ''}. Each one joins this bridge's mesh, one at a time. This changes each device's mesh configuration. Pairing a controller does not assign its buttons or enable Home Assistant events.</p>
  ${sel.some(f => [81,91,126,127].includes(rowType(f))) ? '<p>Controller pairing and programming still need physical qualification on the Pi. Battery controllers must be awake. Configure assignments separately after pairing.</p>' : ''}
  ${sel.some(f => rowType(f) === 81) ? '<p>Anyplace pairing may ask for another wake after exchanging keys. When prompted, press the small Wake-Up button on the bottom once so the bridge can confirm the new key.</p>' : ''}
  ${S.wiz.room ? `<p>They'll be put in <b>${esc(S.wiz.room)}</b>.</p>` : ''}
  <div class="row"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn primary" onclick="closeModal();startClaims()">Pair</button></div>`);
}
async function startClaims() {
  const w = S.wiz;
  if (!w || w.step !== 1 || w.searching || !managementFresh() || !S.status?.mesh_ready || S.status.pending_transactions) return;
  const selected = wizRows().filter(f => f.sel && rowReady(f));
  if (selected.length > pairingRemaining()) { toast(`Only ${pairingRemaining()} pairing attempts are available.`); return; }
  if (!selected.length) { toast('No recently seen devices are selected. Search again first.'); return; }
  let pairingNames;
  try { pairingNames = wizardPairingNames(selected); } catch (e) { toast(e.message); return; }
  w.claims = selected.map(f => ({uuid: f.uuid, uuid_hash: f.uuid_hash, hash_hex: f.hash_hex, type: rowType(f),
    name: pairingNames.get(f.uuid) || `New ${TYPE_NAMES[rowType(f)]} ${f.hash_hex.slice(-4)}`, autoNamed:pairingNames.has(f.uuid), lastSeen:f.lastSeen, epoch:f.epoch, st: 'wait', msg: 'Waiting'}));
  const queueInstance = S.status.instance;
  w.step = 2; w.stop = false; render();
  for (const c of w.claims) {
    if (w.stop) { c.msg = 'Not started'; continue; }
    if (queueInstance !== S.status.instance || Date.now() - c.lastSeen < 0 || Date.now() - c.lastSeen > 300000) { c.st = 'skip'; c.msg = 'Not started: this search result expired or the bridge connection changed. Search again.'; w.stop = true; continue; }
    c.st = 'run'; c.instance = S.status.instance;
    c.msg = 'Pairing… keep the device powered and awake. This can take about a minute.'; render();
    try {
      // Exactly one request per device. A failure is reported, never resent.
      const res = await radio(`Pairing ${c.name}`, step => { step('Exchanging keys'); return api('/api/claim', {uuid: c.uuid, name: c.name, product_id: c.type, setup_ha: false}); });
      c.id = res.device_id; c.st = 'done'; c.receipt = res.requires_review ? res.transaction : null;
      if (c.autoNamed) {
        const a = wizardNaming(); w.names[c.id] = c.name;
        a.generated[c.id] = c.name; a.originals[c.id] = c.name;
      }
      c.msg = res.requires_review ? `Paired as #${res.device_id}, but the bridge wants a review. See Operations under Settings.`
        : `Paired as #${res.device_id} · the device confirmed its new key`;
      log('system', `Paired ${TYPE_NAMES[c.type]} as #${res.device_id}`, res.requires_review ? 'Needs review' : 'Key confirmed by an encrypted reply');
      if (res.requires_review) w.stop = true;
      await refreshSnapshot();
      if (w.room.trim() && feat('labels')) await saveLabels({[c.id]: {room: w.room.trim()}}, true);
    } catch (e) { w.stop = true; c.st = 'fail'; c.msg = e.message; c.receipt = e.details?.transaction; log('system', `Pairing ${c.hash_hex} failed`, e.message); }
    render();
  }
  w.claims.filter(c => c.st === 'wait').forEach(c => { c.st = 'skip'; c.msg = 'Not started'; });
  render();
}
async function finishWizard() {
  const w = S.wiz, entries = {};
  if (!w || w.step !== 3 || w.saving) return;
  if (wizardNaming().enabled && wizardNaming().error) { toast(wizardNaming().error); return; }
  if (wizardNameConflict()) { toast('A suggested name is now in use. Update the prefix or starting number and review the names before finishing.'); return; }
  if (Object.values(w.roomDrafts || {}).some(d => d.editing)) { toast('Add or cancel the new room before finishing.'); return; }
  w.claims.filter(c => c.st === 'done').forEach(c => {
    const v = (w.names[c.id] || '').trim(), room = w.roomDrafts?.[c.id]?.room;
    const entry = {};
    if (v && v !== dev(c.id)?.name) entry.name = v;
    if (feat('labels') && room !== undefined && room !== roomOf(dev(c.id) || {})) entry.room = room;
    if (Object.keys(entry).length) entries[c.id] = entry;
  });
  w.saving = true; render();
  if (Object.keys(entries).length && !(await saveLabels(entries, true))) { w.saving = false; render(); return; }
  const n = w.claims.filter(c => c.st === 'done').length;
  if (S.wiz !== w) return;
  S.wiz = null; go('home'); toast(`Added ${n} device${n === 1 ? '' : 's'}`);
}

/* ---------- native scenes ---------- */
// Drafts live outside snapshots. Scene writes are submitted once; an interrupted
// request is inspected through the journal, never automatically sent again.
let sceneDraft = null, sceneRequest = false, sceneRequestError = '', sceneRefreshBusy = false;
// sceneLast: the request this page started, kept so an owner can repeat a READ-ONLY
// or never-started step without one unpowered light. sceneWatch: the job this page
// saw running, so an old "sent" message is not shown again on a later visit.
let sceneLast = null, sceneWatch = null, sceneDismissed = null;
// sceneSettled: the last job seen finished. sceneEpoch counts scene data shown from outside
// the live stream, so the stream's next update redraws even when its own content is unchanged.
let sceneSettled = null, scenePollTimer = null, sceneEpoch = 0;
const sceneComparisonOpen = new Set();
const sceneWorkflow = () => S.sceneWorkflow || {scenes:[], operations:[], capabilities:{}};
const sceneItems = () => (sceneWorkflow().scenes || []).filter(s => s.status !== 'removed');
const sceneById = id => sceneItems().find(s => String(s.id) === String(id));
const sceneReady = s => s && ['ready','verified'].includes(s.status);
const sceneActivationReady = s => sceneReady(s) && s.activation_ready !== false;
const sceneArg = id => esc(JSON.stringify(String(id)));
const sceneRunning = () => sceneRequest || sceneWorkflow().job?.state === 'running';
const sceneUnfinished = () => (sceneWorkflow().operations || []).filter(o => !['complete','completed','cancelled','verified','resolved','removed'].includes(o.state || o.status));
function sceneCanWrite() { return feat('scenes') && sceneWorkflow().enabled !== false && managementFresh() && S.status?.mesh_ready && !S.radio?.label && !(S.radio?.queue || []).length && !Object.values(S.status?.jobs || {}).some(j => j.state === 'running') && !sceneRunning() && !sceneUnfinished().length && !S.status?.pending_transactions; }
function sceneLights() {
  const caps = sceneWorkflow().capabilities || {}, models = caps.supported_models || [93,134,137,162];
  return lights().filter(d => models.includes(d.product_type)).sort(byName);
}
function sceneLabelStatus(scene) {
  if (sceneReady(scene)) return sceneActivationReady(scene) ? 'Verified' : 'Check saved settings';
  return ({needs_review:'Needs review',programming:'Saving',removing:'Removing',uncertain:'Needs review',pending:'Pending'})[scene.status] || 'Needs review';
}
// An activation finishes in well under a second, so a reply read while the job was still
// running can arrive after a newer update. A job seen finished never goes back to running.
function sceneAccept(workflow) {
  const job = workflow?.job;
  if (job && job.id != null) {
    if (job.state !== 'running') sceneSettled = job.id;
    else if (sceneSettled === job.id) return false;
  }
  S.sceneWorkflow = workflow; return true;
}
function sceneShown() { sceneEpoch++; sceneWatchRunning(); }
// The live stream normally reports the end of a job. While one is running the page also asks
// directly, so a late, silent or refused stream cannot leave it waiting. Reads only.
function sceneWatchRunning() {
  const job = sceneWorkflow().job;
  if (scenePollTimer || job?.state !== 'running' || typeof setTimeout !== 'function') return;
  scenePollTimer = setTimeout(async () => { scenePollTimer = null; await refreshScenes(true); sceneWatchRunning(); },
    String(job.operation || '').startsWith('activate') ? 400 : 1500);
}
async function refreshScenes(quiet = false) {
  if (sceneRefreshBusy || !feat('scenes')) return;
  sceneRefreshBusy = true;
  try { sceneAccept(await getJson('/api/scenes')); }
  catch (e) { if (!quiet) sceneRequestError = e.message; }
  finally { sceneRefreshBusy = false; sceneShown(); renderSoon(); }
}
function openScene(id = null) {
  const found = id === null ? null : sceneById(id); if (id !== null && !found) return;
  sceneDraft = {id:found?.id ?? null, name:found?.name || '', exposed:found?.exposed === true, members:{}, error:'', dirty:false};
  for (const m of found?.members || []) sceneDraft.members[m.target] = {target:m.target, percent:String(pct(m.brightness)), kelvin:String(m.kelvin)};
  sceneRequestError = ''; openDrawer('scene', id ?? 'new');
}
function sceneChangeName(value) { if (sceneDraft && !sceneRunning()) { sceneDraft.name = value; sceneDraft.dirty = true; } }
function sceneToggleMember(target, enabled) {
  if (!sceneDraft || sceneRunning()) return;
  if (enabled) {
    const d = sceneLights().find(d => d.avion_id === target); if (!d) return;
    sceneDraft.members[target] ||= {target, percent:String(known(d) ? pct(d.brightness) : 100), kelvin:String(d.color_temp || 4000)};
  } else delete sceneDraft.members[target];
  sceneDraft.dirty = true; render();
}
function sceneMemberChange(target, key, value) {
  if (!sceneDraft?.members[target] || sceneRunning() || !['percent','kelvin'].includes(key)) return;
  sceneDraft.members[target][key] = value; sceneDraft.dirty = true;
}
function sceneChooseGroup(gid) {
  if (!sceneDraft || sceneRunning()) return;
  for (const d of sceneLights().filter(d => members(gid).some(m => m.avion_id === d.avion_id)))
    sceneDraft.members[d.avion_id] ||= {target:d.avion_id,percent:String(known(d) ? pct(d.brightness) : 100),kelvin:String(d.color_temp || 4000)};
  sceneDraft.dirty = true; render();
}
function sceneApplyPreset() {
  if (!sceneDraft || sceneRunning()) return;
  const level = $('#sceneBulkLevel'), temperature = $('#sceneBulkWhite');
  if (!level.reportValidity()) return;
  for (const member of Object.values(sceneDraft.members)) {
    member.percent = level.value;
    member.kelvin = temperature.value;
    const memberLevel = document.getElementById('sceneLevel' + member.target);
    const memberWhite = document.getElementById('sceneWhite' + member.target);
    if (memberLevel) memberLevel.value = member.percent;
    if (memberWhite) memberWhite.value = member.kelvin;
  }
  sceneDraft.dirty = true;
  render();
}
function scenePayload() {
  const draft = sceneDraft;
  if (!draft || !validLabel(draft.name.trim(),128)) throw Error('Enter a scene name with 1–128 printable characters.');
  const selected = Object.values(draft.members);
  if (!selected.length || selected.length > 32) throw Error('Choose between 1 and 32 lights.');
  const available = new Set(sceneLights().map(d => d.avion_id));
  const entries = selected.map(m => {
    if (!available.has(m.target)) throw Error('A selected light is no longer available. Remove it from this scene before saving.');
    if (!/^\d+$/.test(m.percent) || +m.percent < 0 || +m.percent > 100) throw Error('Brightness must be a whole number from 0 to 100.');
    if (!/^\d+$/.test(m.kelvin) || +m.kelvin < 2700 || +m.kelvin > 5000) throw Error('White temperature must be between 2700 and 5000 K.');
    return {target:m.target, brightness:raw(+m.percent), kelvin:+m.kelvin};
  });
  return {name:draft.name.trim(),members:entries};
}
async function sceneSubmit(action, body) {
  if (sceneRunning()) return false;
  sceneRequest = true; sceneRequestError = ''; sceneDismissed = null; renderSoon();
  try {
    const result = await api('/api/scenes/' + action, body); // Never retry a scene setter.
    if (result.started) {
      sceneLast = {job:result.job?.id ?? null, action, body};
      sceneWatch = {id:result.job?.id ?? null, doneAt:0}; // This page started it, so its result is shown here.
      sceneAccept({...sceneWorkflow(),job:{id:result.job?.id,state:'running',operation:action}});
    } else toast(['activate','activate-keypad-test','activate-quiet-test'].includes(action) ? 'Scene activation sent. Check the lights.' : action === 'exposure' ? 'Home Assistant sharing updated.'
      : action === 'set-aside' ? 'Set aside. Nothing was sent to the lights.' : action === 'forget' ? 'Scene forgotten. Nothing was sent to the lights.' : 'Scene request accepted.');
    const bridgeOnly = ['exposure','set-aside','forget'].includes(action);
    log('system', 'Scene ' + ({create:'save',edit:'edit',remove:'removal',recover:'recovery',activate:'activation','activate-keypad-test':'activation comparison','activate-quiet-test':'activation without status reads',exposure:'sharing','set-aside':'operation set aside',forget:'forgotten'}[action] || action), bridgeOnly ? 'Saved on the bridge. Nothing was sent to the lights.' : 'Request accepted. Stored values are verified separately.');
    return true;
  } catch (e) {
    // A refusal before anything started is not a reason to go looking for a saved operation.
    const refused = e.details?.operation_started === false || [400,403,501].includes(e.status);
    sceneRequestError = refused ? 'Not started. ' + e.message : e.message + ' Check the saved operation before another change.';
    if (sceneDraft) sceneDraft.error = sceneRequestError;
    return false;
  } finally { sceneRequest = false; await refreshScenes(); sceneShown(); renderSoon(); }
}
const sceneLightName = id => dev(id)?.name || 'Light ' + id;
const sceneOp = transaction => sceneUnfinished().find(o => String(o.transaction || o.id) === String(transaction));
const sceneOpDetail = op => op?.progress || op?.detail || {};
const sceneOpWrites = (op, target) => (sceneOpDetail(op).steps || []).some(s => s.target === target);
function sceneCompletionVisible(job) {
  // Show "sent" or "verified" only for work this page watched, and only briefly.
  if (!job) return false;
  if (job.state === 'running') { sceneWatch = {id:job.id ?? null, doneAt:0}; return false; }
  if (!sceneWatch || sceneWatch.id !== (job.id ?? null)) return false;
  if (!sceneWatch.doneAt) { sceneWatch.doneAt = Date.now(); if (typeof setTimeout === 'function') setTimeout(renderSoon, 60100); }
  return Date.now() - sceneWatch.doneAt < 60000;
}
function sceneUncheckedRetry() {
  // Offered only when nothing was written: no saved operation exists for the failed request.
  const job = sceneWorkflow().job, last = sceneLast;
  if (!last || job?.state !== 'failed' || !Number.isInteger(job.unreachable) || last.job !== (job.id ?? null) ||
      sceneUnfinished().length || !['create','edit','remove','verify'].includes(last.action)) return null;
  const scene = last.body.id ? sceneById(last.body.id) : null, target = job.unreachable;
  const before = scene?.members?.find(m => m.target === target);
  const after = last.body.members?.find(m => m.target === target);
  // Match the engine's write set: adding, removing or changing a preset needs a
  // reply; an unchanged member may be left unread just like a nonmember.
  const written = last.action === 'verify' ? false
    : last.action === 'remove' ? !scene || !!before
    : last.action === 'edit' ? !scene || !!before !== !!after || (!!before &&
        (before.brightness !== after.brightness || before.kelvin !== after.kelvin))
    : !!after;
  return {target, written, action:last.action};
}
async function sceneCarryOnUnchecked() {
  const offer = sceneUncheckedRetry(), last = sceneLast;
  if (!offer || offer.written || !managementFresh() || !S.status?.mesh_ready || sceneRunning()) return;
  if (offer.action !== 'verify' && !sceneCanWrite()) return;
  await sceneSubmit(last.action, {...last.body, skip:[...new Set([...(last.body.skip || []), offer.target])]});
}
async function recoverSceneUnchecked(transaction, target) {
  const op = sceneOp(transaction);
  if (!op || !Number.isInteger(target) || sceneOpWrites(op, target) || !managementFresh() || !S.status?.mesh_ready || sceneRunning()) return;
  await sceneSubmit('recover', {transaction, skip:[...new Set([...(sceneOpDetail(op).assumed || []), target])]});
}
function confirmSetAsideScene(transaction) {
  const op = sceneOp(transaction), p = sceneOpDetail(op);
  if (!op || p.legacy_ambiguous || sceneRunning() || !managementFresh()) return;
  modal(`<h3>Set this scene ${p.action === 'remove' ? 'removal' : 'save'} aside?</h3>
    <p>Use this when a light cannot answer and the ${p.action === 'remove' ? 'removal' : 'save'} cannot be finished. The bridge stops waiting and sends nothing to the lights.</p>
    <ul class="scene-steps"><li>A light that already received the change keeps it. The bridge reserves that scene slot and does not use it again.</li>
    <li>${p.prior ? 'The scene stays in your list. It cannot be activated until Verify saved settings passes. If that no longer passes, forget the scene and create it again.' : 'The new scene is not added. You can create it again.'}</li>
    <li>Pairing, removal and other changes become available again. The record stays in Operations.</li></ul>
    <div class="row"><button class="btn" onclick="closeModal()">Keep waiting</button><button class="btn danger" onclick="setAsideScene(${sceneArg(transaction)})">Set aside</button></div>`);
}
async function sceneBridgeOnly(action, body) {
  closeModal();
  const ok = await sceneSubmit(action, body);
  if (typeof refreshSnapshot === 'function') await refreshSnapshot();
  if (typeof Ops !== 'undefined' && Ops.list !== null && typeof loadOperations === 'function') loadOperations();
  return ok;
}
async function setAsideScene(transaction) {
  if (!sceneOp(transaction) || sceneRunning() || !managementFresh()) return;
  await sceneBridgeOnly('set-aside', {transaction});
}
function confirmForgetScene(id) {
  const scene = sceneById(id); if (!scene || sceneRunning() || !managementFresh() || sceneUnfinished().some(o => o.scene_id === scene.id)) return;
  modal(`<h3>Forget ${esc(scene.name)}?</h3><p>The bridge removes this scene from its list and from Home Assistant. Nothing is sent to the lights, so they keep its saved settings in a slot the bridge does not use again.</p><p class="hint">When the lights can be reached, use Remove scene instead. It clears the settings from the lights.</p><div class="row"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn danger" onclick="forgetScene(${sceneArg(id)})">Forget scene</button></div>`);
}
async function forgetScene(id) {
  if (!sceneById(id) || sceneRunning() || !managementFresh()) return;
  const draft = sceneDraft;
  if (await sceneBridgeOnly('forget', {id}) && sceneDraft === draft && S.drawer?.kind === 'scene') { sceneDraft = null; closeDrawer(true); }
}
async function saveScene() {
  if (!sceneCanWrite() || !sceneDraft) return;
  let body; try { body = scenePayload(); } catch (e) { sceneDraft.error = e.message; render(); return; }
  const draft = sceneDraft, action = draft.id === null ? 'create' : 'edit';
  if (draft.id === null) body.exposed = draft.exposed; else body.id = draft.id;
  const accepted = await sceneSubmit(action, body);
  if (accepted && sceneDraft === draft) { sceneDraft = null; closeDrawer(); }
}
async function activateScene(id) {
  if (!sceneCanWrite() || !sceneActivationReady(sceneById(id))) return;
  await sceneSubmit('activate',{id});
}
async function testSceneActivation(id) {
  if (!sceneCanWrite() || !sceneActivationReady(sceneById(id)) || !sceneDraft || String(sceneDraft.id) !== String(id) || sceneDraft.dirty) return;
  await sceneSubmit('activate-keypad-test',{id});
}
async function testQuietSceneActivation(id) {
  if (!sceneCanWrite() || !sceneActivationReady(sceneById(id)) || !sceneDraft || String(sceneDraft.id) !== String(id) || sceneDraft.dirty) return;
  await sceneSubmit('activate-quiet-test',{id});
}
function confirmRemoveScene(id) {
  const scene = sceneById(id); if (!scene || !sceneCanWrite()) return;
  modal(`<h3>Remove ${esc(scene.name)}?</h3><p>This clears both saved settings from each light in this scene, then checks they are gone. The lights stay paired.</p><div class="row"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn danger" onclick="removeScene(${sceneArg(id)})">Remove scene</button></div>`);
}
async function removeScene(id) { if (!sceneCanWrite()) return; closeModal(); await sceneSubmit('remove',{id}); }
async function recoverScene(transaction) {
  if (!managementFresh() || !S.status?.mesh_ready || sceneRunning()) return;
  await sceneSubmit('recover',{transaction});
}
async function resumeScene(transaction) {
  const op = sceneUnfinished().find(o => String(o.transaction || o.id) === String(transaction));
  if (!managementFresh() || !S.status?.mesh_ready || sceneRunning() || (op?.progress || op?.detail)?.recovery_ready !== true) return;
  await sceneSubmit('resume',{transaction});
}
async function verifyScene(id) {
  if (!managementFresh() || !S.status?.mesh_ready || sceneRunning() || sceneUnfinished().length) return;
  await sceneSubmit('verify',{id});
}
async function shareScene(id, exposed) {
  if (!sceneCanWrite() || !sceneReady(sceneById(id))) return;
  await sceneSubmit('exposure',{id,exposed});
}
function sceneLightProgress(steps, unconfirmed = false) {
  const grouped = new Map();
  for (const step of Array.isArray(steps) ? steps : []) {
    if (!grouped.has(step.target)) grouped.set(step.target, []);
    grouped.get(step.target).push(step);
  }
  return [...grouped].map(([target, entries]) => {
    const properties = new Set(entries.map(s => s.property)), removal = properties.has('remove');
    const fullPreset = properties.has('preset') || properties.has('colour') && properties.has('brightness');
    const verified = !unconfirmed && entries.every(s => (s.status || s.state) === 'verified') && (removal || fullPreset);
    const waiting = entries.every(s => ['not_started','waiting'].includes(s.status || s.state || 'waiting'));
    const text = verified ? removal ? 'Preset removal verified' : 'Full preset verified'
      : waiting ? removal ? 'Waiting to remove preset' : 'Waiting to save full preset'
      : removal ? 'Preset removal not yet confirmed' : 'Full preset not yet confirmed';
    return `<li>${esc(dev(target)?.name || 'Light ' + target)} · ${text}</li>`;
  }).join('');
}
function sceneProgressHtml() {
  const workflow = sceneWorkflow(), job = workflow.job, pending = sceneUnfinished();
  const running = job?.state === 'running';
  const done = job?.state === 'complete' && sceneCompletionVisible(job) ? job.result?.desired_matched === false
    ? 'Readback found the previous readable settings. The requested full preset was not confirmed.'
    : job.result?.resume_available ? 'Readback is complete. Review the remaining steps and choose Continue save.'
    : ['activate','activate-keypad-test','activate-quiet-test'].includes(job.operation) ? 'Scene activation sent. Check the lights to confirm the result.'
    : job.result?.verified ? job.operation === 'remove' ? 'Scene removal verified on its lights.' : job.result?.unchecked?.length || job.result?.scene && sceneById(job.result.scene.id)?.unchecked?.length ? 'Saved scene settings verified on the lights that were checked.' : 'Saved scene settings verified on the lights.' : '' : '';
  if (running) sceneCompletionVisible(job);
  const failed = job?.state === 'failed' && (job.id ?? 'job') !== sceneDismissed;
  const error = sceneRequestError || (failed ? job.error?.message || job.error || 'The scene operation needs review.' : '');
  const offer = failed ? sceneUncheckedRetry() : null, offerName = offer ? esc(sceneLightName(offer.target)) : '';
  const offerHtml = !offer ? '' : offer.written
    ? `<p class="hint">This request changes ${offerName}, so it must answer. Check that it has power, then try again. Nothing was changed.</p>`
    : `<p class="hint">Nothing was changed. If ${offerName} is switched off, you can carry on without checking it. The scene is then not checked on that light until you verify it again.</p><p class="hint"><strong>When powered again, ${offerName} could respond when you activate this scene, even if you did not select it, if it already holds settings in the same scene slot.</strong> Verify saved settings once it has power.</p><button class="btn" onclick="sceneCarryOnUnchecked()" ${sceneRunning() || !S.status?.mesh_ready ? 'disabled' : ''}>Carry on without ${offerName}</button>`;
  return `<div id="sceneLiveProgress" aria-live="polite">${workflow.error ? `<div class="callout warn">${I.alert}<div>${esc(workflow.error)}</div></div>` : ''}${done ? `<p class="hint" role="status">${esc(done)}</p>` : ''}${running ? `<div class="callout info">${I.info}<div><b>${esc({create:'Saving scene',edit:'Updating scene',remove:'Removing scene',recover:'Reading saved settings',resume:'Continuing scene save',verify:'Verifying saved settings',activate:'Activating scene','activate-keypad-test':'Comparing scene activation','activate-quiet-test':'Checking scene response'}[job.operation] || 'Working on scene')}…</b><p class="hint">${job.operation === 'activate-quiet-test' ? 'Watch the lights now. Automatic status reads pause for three seconds, so the display may update later.' : ['activate','activate-keypad-test'].includes(job.operation) ? 'Sending one command to recall the saved scene.' : "Each light's full preset is checked together: brightness and white temperature. Larger installations take longer. You can leave this page and return."}</p></div></div>` : ''}
    ${error ? `<div class="callout warn">${I.alert}<div><p>${esc(typeof error === 'string' ? error : JSON.stringify(error))}</p>${offerHtml}${failed && !sceneRequestError && !pending.length ? `<button class="btn sm" onclick="sceneDismissed=${sceneArg(job.id ?? 'job')};render()">Dismiss</button>` : ''}</div></div>` : ''}
    ${pending.map(op => {
      const transaction = op.transaction || op.id, p = op.progress || op.detail || {}, steps = p.steps || op.steps || [];
      const silent = Number.isInteger(p.unreachable) ? p.unreachable : null, silentName = silent === null ? '' : esc(sceneLightName(silent));
      const waiting = sceneRunning() || !S.status?.mesh_ready ? 'disabled' : '';
      return `<div class="card scene-operation"><h3>${esc(sceneById(op.scene_id)?.name || p.name || 'Scene operation')} · ${running ? 'In progress' : 'Needs review'}</h3>
      <p>${running ? 'Checking full light presets' : 'Full preset verification needs review'}${p.target ? ' · ' + esc(dev(p.target)?.name || `Light ${p.target}`) : ''}</p>
      ${p.legacy_ambiguous ? `<p class="hint">An earlier save may have sent only part of a light's preset. The full preset is not confirmed. Read and recover checks stored settings without sending scene changes.${p.legacy_conflict ? ' Earlier saved attempts disagree, so continuation remains blocked.' : ''}</p>` : ''}
      ${Array.isArray(steps) && steps.length ? `<ul class="scene-steps">${sceneLightProgress(steps, p.legacy_ambiguous === true)}</ul>` : ''}
      ${p.assumed?.length ? `<p class="hint">Left unchecked because ${p.assumed.length === 1 ? 'it was' : 'they were'} switched off: ${esc(p.assumed.map(sceneLightName).join(', '))}.</p>` : ''}
      ${!running && transaction && p.recovery_ready === true ? `<p class="hint">${p.recovery_mode === 'continue_unattempted' ? 'The full preset is not confirmed yet. Continue save sends only the remaining part that was never attempted, then verifies both settings.' : p.retry_requires_explicit_action ? 'Readback found the previous readable settings. Retrying sends the full preset again and checks both settings before continuing.' : 'Continue save finishes the remaining light presets and verifies both settings.'} Nothing runs automatically.</p><button class="btn primary" onclick="resumeScene(${sceneArg(transaction)})" ${waiting}>${p.retry_requires_explicit_action ? 'Retry full preset and continue' : 'Continue save'}</button>` : ''}
      ${!running && transaction ? `<p class="hint">Recovery reads the lights and checks what was saved. It does not repeat an uncertain write.</p><button class="btn" onclick="recoverScene(${sceneArg(transaction)})" ${waiting}>Read and recover</button>` : ''}
      ${!running && transaction && silent !== null ? sceneOpWrites(op, silent)
        ? `<p class="hint">${silentName} did not answer and is part of this save, so it must be read. Check that it has power, then choose Read and recover. If it cannot be powered, set this save aside.</p>`
        : `<p class="hint">${silentName} did not answer. This save never changes that light, so recovery can go on without reading it.</p><p class="hint"><strong>When powered again, ${silentName} could respond when you activate this scene, even if you did not select it, if it already holds settings in the same scene slot.</strong> Verify saved settings once it has power.</p><button class="btn" onclick="recoverSceneUnchecked(${sceneArg(transaction)},${silent})" ${waiting}>Read and recover without ${silentName}</button>` : ''}
      ${!running && transaction && !p.legacy_ambiguous ? `<p class="hint">If a light cannot answer, you can stop waiting. Setting aside sends nothing and frees the bridge for other changes.</p><button class="btn" onclick="confirmSetAsideScene(${sceneArg(transaction)})" ${sceneRunning() || !managementFresh() ? 'disabled' : ''}>Set aside…</button>` : ''}</div>`;
    }).join('')}</div>`;
}
function viewScenes() {
  const scenes = sceneItems(), reserved = (sceneWorkflow().reserved_references || []).length;
  return `<div class="view-head"><div><h2>Scenes</h2><div class="sub">Save brightness and white temperature for one light or several lights.</div></div><div class="actions"><button class="btn primary" onclick="openScene()" ${sceneCanWrite() ? '' : 'disabled'}>${I.plus}Create scene</button></div></div>
  <p class="hint">Scenes are stored in the lights themselves. Light types marked Testing in the editor have had less checking on real lights, so activate a new scene once after saving it.${reserved ? ` ${reserved} scene slot${reserved === 1 ? ' is' : 's are'} reserved after a set-aside save or a forgotten scene.` : ''}</p>
  ${sceneProgressHtml()}
  <div class="scene-grid">${scenes.map(scene => `<article class="card scene-card"><h3>${esc(scene.name)}</h3><p>${scene.members?.length || 0} light${scene.members?.length === 1 ? '' : 's'} · <span class="badge ${sceneActivationReady(scene) ? 'ok' : 'warn'}">${esc(sceneLabelStatus(scene))}</span></p>
    <p class="small muted">${scene.exposed ? 'Shared with Home Assistant' : 'Available on this bridge only'}</p>
    ${sceneReady(scene) && !sceneActivationReady(scene) ? '<p class="hint">Open Edit / details and choose Verify saved settings before activating. Verification does not turn the lights on.</p>' : ''}
    ${scene.set_aside ? '<p class="hint">A change to this scene was set aside. Open Edit / details and choose Verify saved settings to see whether the lights still hold the earlier settings.</p>' : ''}
    ${scene.unchecked?.length ? `<p class="hint">Not checked on ${esc(scene.unchecked.map(sceneLightName).join(', '))}. Verify saved settings once ${scene.unchecked.length === 1 ? 'it has' : 'they have'} power.</p>` : ''}
    <div class="row"><button class="btn primary" onclick="activateScene(${sceneArg(scene.id)})" ${sceneCanWrite() && sceneActivationReady(scene) ? '' : 'disabled'}>Activate</button><button class="btn" onclick="openScene(${sceneArg(scene.id)})">Edit / details</button></div></article>`).join('') || '<p class="empty">No scenes yet. Create a scene and choose the settings for each light.</p>'}</div>
  ${!S.status?.mesh_ready ? '<p class="hint">Connect to the lighting mesh before changing scenes.</p>' : ''}`;
}
function sceneDrawer() {
  const draft = sceneDraft; if (!draft) return '';
  const existing = draft.id === null ? null : sceneById(draft.id), busy = sceneRunning(), editable = sceneCanWrite();
  const eligible = sceneLights(), missing = Object.values(draft.members).filter(m => !eligible.some(d => d.avion_id === m.target));
  const temps = [[2700,'2700 K · Warm'],[3000,'3000 K'],[3500,'3500 K · Neutral'],[4000,'4000 K'],[5000,'5000 K · Cool']];
  return `<div class="drawer-head scene-head">${I.layers}<div class="t"><h3>${draft.id === null ? 'Create scene' : esc(existing?.name || draft.name)}</h3><div class="small muted">Choose a preset for each light</div></div><button class="icon-btn" aria-label="Close" onclick="closeDrawer()">${I.close}</button></div>
  <div class="drawer-body scene-editor">${sceneProgressHtml()}
    <div class="field"><label for="sceneName">Scene name</label><input class="inp" id="sceneName" type="text" maxlength="128" value="${esc(draft.name)}" oninput="sceneChangeName(this.value)" ${busy ? 'disabled' : ''}></div>
    <div class="sec" data-scene-key="presets"><h4>Lights and presets</h4><p class="hint">Select the lights to include. Each light can have different settings. Saving programs these settings; use Activate afterwards to test them.</p>
    ${lightingGroups().length ? `<div class="scene-group-shortcuts">${lightingGroups().map(g => `<button class="chip" onclick="sceneChooseGroup(${g.group_id})" ${busy ? 'disabled' : ''}>Add ${esc(g.name)} lights</button>`).join('')}</div>` : ''}
    <p class="small muted" role="status" data-scene-key="selection-count">${Object.keys(draft.members).length} of ${eligible.length} lights selected</p>
    ${Object.keys(draft.members).length > 1 ? `<fieldset class="scene-bulk" data-scene-key="bulk" ${busy ? 'disabled' : ''}>
      <legend>Preset for selected lights</legend><div class="scene-values">
      <div class="field"><label for="sceneBulkLevel">Brightness (%)</label><input id="sceneBulkLevel" class="inp" type="number" inputmode="numeric" min="0" max="100" required step="1" value="${esc(draft.bulkLevel ?? '100')}" oninput="sceneDraft.bulkLevel=this.value"></div>
      <div class="field"><label for="sceneBulkWhite">White temperature</label><select id="sceneBulkWhite" class="inp" onchange="sceneDraft.bulkWhite=this.value">${temps.map(([k,label]) => `<option value="${k}" ${String(k) === (draft.bulkWhite ?? '4000') ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select></div></div>
      <button type="button" class="btn" onclick="sceneApplyPreset()">Apply to selected lights</button>
      <p class="hint">Adjust individual lights below before saving.</p></fieldset>` : ''}
    <div class="scene-members">${eligible.map(d => { const m = draft.members[d.avion_id], currentTemps = m && !temps.some(t => t[0] === +m.kelvin) ? [[+m.kelvin,m.kelvin+' K'],...temps] : temps;
      return `<div class="scene-member" data-scene-key="light-${d.avion_id}"><label class="scene-select"><input type="checkbox" ${m ? 'checked' : ''} onchange="sceneToggleMember(${d.avion_id},this.checked)" ${busy ? 'disabled' : ''}><span><b>${esc(d.name)}</b><span class="small muted">${esc(roomOf(d) || typeName(d))}${d.product_type === 134 ? '' : ' · Testing'}</span></span></label>
      ${m ? `<div class="scene-values"><div class="field"><label for="sceneLevel${d.avion_id}">Brightness (%)</label><input id="sceneLevel${d.avion_id}" class="inp" type="number" inputmode="numeric" min="0" max="100" step="1" value="${esc(m.percent)}" oninput="sceneMemberChange(${d.avion_id},'percent',this.value)" ${busy ? 'disabled' : ''}></div><div class="field"><label for="sceneWhite${d.avion_id}">White temperature</label><select id="sceneWhite${d.avion_id}" class="inp" onchange="sceneMemberChange(${d.avion_id},'kelvin',this.value)" ${busy ? 'disabled' : ''}>${currentTemps.map(([k,label]) => `<option value="${k}" ${+m.kelvin === k ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select></div></div>` : ''}</div>`;
    }).join('') || '<p class="hint">No compatible white-temperature lights are available.</p>'}
    ${missing.map(m => `<div class="scene-member" data-scene-key="missing-${m.target}"><p>Light #${m.target} is no longer available.</p><button class="btn" onclick="sceneToggleMember(${m.target},false)">Remove from scene</button></div>`).join('')}</div></div>
    <div class="sec" data-scene-key="ha"><h4>Home Assistant</h4>${draft.id === null ? `<label class="scene-select"><input type="checkbox" ${draft.exposed ? 'checked' : ''} onchange="sceneDraft.exposed=this.checked;sceneDraft.dirty=true" ${busy ? 'disabled' : ''}><span>Add this scene to Home Assistant after verification</span></label>` : `<p>${existing?.exposed ? 'Shared with Home Assistant.' : 'Available on this bridge only.'} ${S.health?.mqtt_connected ? 'MQTT is connected.' : 'Configure MQTT in Settings to use scenes in Home Assistant.'}</p><button class="btn" onclick="shareScene(${sceneArg(draft.id)},${!existing?.exposed})" ${editable && sceneReady(existing) ? '' : 'disabled'}>${existing?.exposed ? 'Remove from Home Assistant' : 'Add to Home Assistant'}</button>`}<p class="hint">Home Assistant activates the saved preset through MQTT. A scene has an Activate action, not an on/off state.</p></div>
    ${draft.error ? `<p role="alert" class="scene-error">${esc(draft.error)}</p>` : ''}
    ${existing ? `<div class="sec" data-scene-key="comparison"><details ${sceneComparisonOpen.has(String(draft.id)) ? 'open' : ''} ontoggle="this.open ? sceneComparisonOpen.add(${sceneArg(draft.id)}) : sceneComparisonOpen.delete(${sceneArg(draft.id)})"><summary>Activation troubleshooting</summary><p class="hint">Try a scene command observed from a native keypad. Compare response time, brightness and white temperature with normal activation. This is an experimental one-time action for the saved scene.</p><button class="btn" onclick="testSceneActivation(${sceneArg(draft.id)})" ${editable && sceneActivationReady(existing) && !draft.dirty ? '' : 'disabled'}>Test alternative activation</button><p class="hint">Send the normal scene command, then pause automatic status reads for three seconds. Watch when the lights start changing; the display may update later.</p><button class="btn" onclick="testQuietSceneActivation(${sceneArg(draft.id)})" ${editable && sceneActivationReady(existing) && !draft.dirty ? '' : 'disabled'}>Test without status reads</button>${draft.dirty ? '<p class="hint">Save your changes before comparing activation.</p>' : ''}</details></div>` : ''}
    ${existing ? `<div class="sec" data-scene-key="verify"><h4>Check saved settings</h4><p class="hint">Read both settings from every light. This does not program or activate the scene.</p><button class="btn" onclick="verifyScene(${sceneArg(draft.id)})" ${!busy && !sceneUnfinished().length && S.status?.mesh_ready ? '' : 'disabled'}>Verify saved settings</button></div><div class="sec" data-scene-key="remove"><h4>Remove scene</h4><p class="hint">Clear the scene from its lights and remove its Home Assistant entry.</p><button class="btn danger" onclick="confirmRemoveScene(${sceneArg(draft.id)})" ${editable ? '' : 'disabled'}>Remove scene…</button><p class="hint">If its lights cannot be reached, the bridge can forget this scene without contacting them.</p><button class="btn" onclick="confirmForgetScene(${sceneArg(draft.id)})" ${!busy && managementFresh() && !sceneUnfinished().some(o => o.scene_id === draft.id) ? '' : 'disabled'}>Forget scene…</button></div>` : ''}
  </div><div class="drawer-foot"><button class="btn" onclick="closeDrawer()">Close</button><button class="btn primary" onclick="saveScene()" ${editable ? '' : 'disabled'}>${draft.id === null ? 'Save scene' : 'Save changes'}</button></div>`;
}
function updateSceneProgress() {
  // Preserve focused scene controls while replacing only the live status area.
  const mount = $('#drawerRoot #sceneLiveProgress');
  if (mount && S.drawer?.kind === 'scene') {
    const next = document.createElement('div'); next.innerHTML = sceneProgressHtml();
    syncSceneNode(mount, next.firstElementChild);
  }
}
function sceneNodeKey(node) { return node.nodeType === 1 ? node.id || node.getAttribute('data-scene-key') || '' : ''; }
function sameSceneNode(a, b) { return a && a.nodeType === b.nodeType && a.nodeName === b.nodeName && sceneNodeKey(a) === sceneNodeKey(b); }
function syncSceneNode(current, next) {
  // Scene updates must not detach the scrolling element or unchanged controls:
  // replacing either cancels touch momentum and can reopen native pickers.
  if (current.isEqualNode(next)) return;
  if (current.nodeType !== 1) { current.nodeValue = next.nodeValue; return; }
  for (const a of [...current.attributes]) if (!next.hasAttribute(a.name)) current.removeAttribute(a.name);
  for (const a of [...next.attributes]) if (current.getAttribute(a.name) !== a.value) current.setAttribute(a.name, a.value);
  let cursor = current.firstChild;
  for (const child of [...next.childNodes]) {
    let retained = sameSceneNode(cursor, child) ? cursor : null;
    if (!retained && sceneNodeKey(child)) retained = [...current.childNodes].find(n => sameSceneNode(n, child));
    if (retained) {
      if (retained !== cursor) current.insertBefore(retained, cursor);
      syncSceneNode(retained, child); cursor = retained.nextSibling;
    } else current.insertBefore(child, cursor);
  }
  while (cursor) { const old = cursor; cursor = cursor.nextSibling; old.remove(); }
  if (current.tagName === 'INPUT') {
    if (current.checked !== next.checked) current.checked = next.checked;
    if (current !== document.activeElement && current.value !== next.value) current.value = next.value;
  }
}
function renderSceneDrawer(root) {
  const next = root.cloneNode(false); next.innerHTML = drawerHtml();
  if (root.querySelector('.scene-editor') && root.dataset.sceneId === String(S.drawer.id)) syncSceneNode(root, next);
  else root.replaceChildren(...next.childNodes);
  root.dataset.sceneId = String(S.drawer.id);
}

/* ---------- activity & settings ---------- */
function viewActivity() {
  const F = [['all', 'All'], ['light', 'Lights'], ['system', 'Bridge']], ic = {light: I.bulb, system: I.gear};
  const items = S.log.filter(a => S.actFilter === 'all' || a.kind === S.actFilter);
  return `<div class="view-head"><div><h2>Activity</h2><div class="sub">Since this page was opened, newest first</div></div>
    <div class="actions"><label class="small" style="display:flex;align-items:center;gap:8px"><label class="switch"><input type="checkbox" ${S.showRaw ? 'checked' : ''} onchange="S.showRaw=this.checked;render()"><span></span></label>Show raw data</label></div></div>
  <div class="fseg" style="margin-bottom:12px">${F.map(([id, n]) => `<button aria-pressed="${S.actFilter === id}" onclick="S.actFilter='${id}';render()">${n}</button>`).join('')}</div>
  ${items.length ? `<ul class="feed">${items.map(a => `<li><time>${a.t}</time><span class="ic">${ic[a.kind] || I.gear}</span><div><b style="font-weight:500">${esc(a.title)}</b>${a.detail ? `<div class="small muted">${esc(a.detail)}</div>` : ''}${S.showRaw && a.raw ? `<code class="raw">${esc(a.raw)}</code>` : ''}</div></li>`).join('')}</ul>` : '<p class="empty">Nothing yet.</p>'}
  <p class="hint">Light changes the bridge hears about appear here while this page is open.</p>`;
}
function healthLine() {
  const h = S.health, mq = h ? (h.mqtt_enabled ? (h.mqtt_connected ? 'MQTT connected' : 'MQTT disconnected') : 'MQTT off') : '';
  return `<span class="dot ${S.sse ? '' : 'bad'}"></span>${S.sse ? 'Bridge connected' : 'Reconnecting…'} · Bluetooth ${BLE_NAMES[S.ble] || '?'}${mq ? ' · ' + mq : ''}`;
}
const openCommandTimings = new Set();
function commandTimingCard() {
  const timing = S.status?.command_timings;
  if (!timing) return '';
  const ms = n => Number.isFinite(n) ? `${Math.round(n)} ms` : '—';
  const states = {queued:'Queued', dispatching:'Sending', waiting_reports:'Waiting for reports',
    reported:'Reports received', published:'State published', superseded:'Replaced by a newer command',
    session_changed:'Connection changed', expired:'Measurement ended', not_dispatched:'Not sent',
    dispatch_uncertain:'Send outcome uncertain'};
  const recent = Array.isArray(timing.recent) ? timing.recent : [];
  return `<div class="card"><h3>Control response times</h3>
    <p>Measured on this bridge. MQTT sent means the publish call finished; it does not measure when Home Assistant displays the change. Native timings start when the bridge hears the controller.</p>
    ${recent.length ? `<div class="command-timings">${recent.map(r => `<details ${openCommandTimings.has(r.id) ? 'open' : ''} ontoggle="this.open ? openCommandTimings.add(${r.id}) : openCommandTimings.delete(${r.id})"><summary>${esc((dev(r.target) || grp(r.target))?.name || `Target ${r.target}`)} · ${esc({ui:'Bridge UI',mqtt:'MQTT',native:'Wall control',bridge:'Bridge'}[r.source] || r.source)} · ${esc(states[r.status] || r.status)}</summary>
      <dl class="kv"><dt>Waiting to send</dt><dd>${ms(r.queue_ms)}</dd><dt>Bluetooth send</dt><dd>${ms(r.dispatch_ms)}</dd>
      <dt>First light report</dt><dd>${ms(r.first_report_ms)}</dd><dt>All requested readings</dt><dd>${ms(r.all_reports_ms)} · ${r.members_matched} of ${r.members} lights</dd>
      <dt>MQTT sent</dt><dd>${ms(r.mqtt_sent_ms)}</dd><dt>MQTT queue</dt><dd>${ms(r.mqtt_queue_ms)}</dd><dt>MQTT publish call</dt><dd>${ms(r.mqtt_publish_call_ms)}</dd></dl>
      <p class="hint">Report and MQTT times are measured from command receipt. Matching reports are observations, not proof that a particular command caused them.</p></details>`).join('')}</div>` : '<p class="muted">No light commands measured since this bridge started.</p>'}
  </div>`;
}
function openMqttSettings() {
  go('settings');
  const section = document.querySelector('[data-settings-panel="mqtt"]');
  if (section) {
    section.open = true;
    section.querySelector('summary').focus();
  }
}
function viewSettings() {
  const h = S.health || {}, f = S.caps.features || {};
  return `<div class="view-head"><div><h2>Settings</h2></div></div>
  <div class="cards">
   <div class="card"><h3>Status</h3><dl class="kv" style="margin-top:8px">
     <dt>Bridge</dt><dd><span class="dot ${S.sse ? '' : 'bad'}"></span>${S.sse ? 'Connected' : 'Reconnecting'} · Raspberry Pi · v${esc(S.caps.version || '?')}</dd>
     <dt>Bluetooth</dt><dd><span class="dot ${S.ble === 4 ? '' : S.ble === 5 ? 'bad' : 'warn'}"></span>${BLE_NAMES[S.ble] || '?'}</dd>
     <dt>MQTT broker</dt><dd><span class="dot ${h.mqtt_connected ? '' : h.mqtt_enabled ? 'bad' : 'warn'}"></span>${h.mqtt_enabled ? (h.mqtt_connected ? 'MQTT connected' : 'MQTT not connected') : 'MQTT not configured'}</dd>
     <dt>Inventory</dt><dd>${lights().length} lights, ${controllers().length} controllers, ${lightingGroups().length} groups</dd>
     <dt>Unfinished operations</dt><dd class="mono">${(S.status || {}).pending_transactions ?? '–'}</dd>
     <dt>Publication cleanup</dt><dd class="mono">${(S.status || {}).publication_pending ?? 0}</dd>
     <dt>Messages received</dt><dd class="mono">${S.rx.toLocaleString()}</dd></dl></div>
   ${opsSettingsCards()}
   ${typeof BridgeSettings !== 'undefined' ? BridgeSettings.html() : ''}
   ${f.all_lights ? `<div class="card"><h3>All lights</h3><p>Sends one command to every light on the mesh.</p><div class="row"><button class="btn" onclick="allLights(true)">Turn everything on…</button><button class="btn" onclick="allLights(false)">Turn everything off…</button></div></div>` : ''}
   ${commandTimingCard()}
   ${opsInventoryCard()}
  </div>`;
}

/* ---------- rendering ---------- */
const NAV = [['home', 'Home', I.home], ['devices', 'Devices', I.list], ['groups', 'Groups', I.layers], ['scenes', 'Scenes', I.flash], ['controllers', 'Controllers', I.keypad], ['add', 'Add devices', I.plus], ['activity', 'Activity', I.activity], ['settings', 'Settings', I.gear]];
function renderNav() {
  const n = attentionItems().length;
  $('#navItems').innerHTML = NAV.filter(([id]) => (id !== 'add' || feat('pair_lights')) && (id !== 'scenes' || feat('scenes'))).map(([id, label, ic]) =>
    `<button aria-current="${S.view === id || (id === 'settings' && S.view === 'operations')}" onclick="go('${id}')">${ic}<span>${label}</span>${id === 'home' && n ? `<span class="badge warn">${n}</span>` : ''}</button>`).join('');
  const h = S.health;
  $('#healthBox').innerHTML = `<div class="row"><span><span class="dot ${S.ble === 4 ? '' : S.ble === 5 ? 'bad' : 'warn'}"></span>Bluetooth</span><span class="muted">${BLE_NAMES[S.ble] || '?'}</span></div>
    <div class="row"><span><span class="dot ${h && h.mqtt_connected ? '' : 'warn'}"></span>MQTT broker</span><span class="muted">${h ? (h.mqtt_enabled ? (h.mqtt_connected ? 'Connected' : 'Offline') : 'Off') : '…'}</span></div>
    <div class="row"><span><span class="dot ${S.sse ? '' : 'bad'}"></span>Live updates</span><span class="muted">${S.sse ? 'On' : 'Reconnecting'}</span></div>`;
}
function replaceAround(parent, incoming, retained, placeholder) {
  for (const child of [...parent.childNodes]) if (child !== retained) child.remove();
  let after = false;
  for (const child of [...incoming.childNodes]) {
    if (child === placeholder) { after = true; continue; }
    if (after) parent.append(child); else parent.insertBefore(child, retained);
  }
}
function textEntry(node) { return node?.matches?.('textarea,input:not([type]),input[type=text],input[type=search],input[type=email],input[type=password],input[type=number],input[type=url],input[type=tel]'); }
function renderMain(keepFocus, preserveText = false) {
  const m = $('#main'), ae = document.activeElement, id = ae && m.contains(ae) && ae.id;
  const selection = id ? [ae.selectionStart,ae.selectionEnd,ae.selectionDirection] : null;
  if (!S.synced) { m.innerHTML = `<p class="empty" role="status">${waitingSnapshot ? 'Loading device types…' : S.sse ? 'Loading inventory…' : 'Connecting to the bridge…'}</p>`; return; }
  const viewKey = S.view === 'add' ? 'add:'+S.wiz?.step : S.view, same = m.dataset.view === viewKey;
  if (same && (composingField?.isConnected && m.contains(composingField) || preserveText && id && textEntry(ae) && S.view === 'add' && !S.wiz?.saving)) return;
  const next = document.createElement('div');
  next.innerHTML = {home: viewHome, devices: viewDevices, groups: viewGroups, scenes: viewScenes, controllers: () => viewDevices(true), add: viewAdd, activity: viewActivity, settings: viewSettings, operations: viewOperations}[S.view]();
  const mount = same && S.view === 'settings' && m.querySelector('#bridgeSettingsMount');
  const toolbar = same && ['devices','controllers'].includes(S.view) && m.querySelector('.toolbar');
  if (mount) {
    const incomingMount = next.querySelector('#bridgeSettingsMount'), cards = mount.parentNode, incomingCards = incomingMount.parentNode;
    replaceAround(cards,incomingCards,mount,incomingMount); replaceAround(m,next,cards,incomingCards);
  } else if (toolbar) {
    const incoming = next.querySelector('.toolbar'), input = toolbar.querySelector('#devSearch'), value = incoming.querySelector('#devSearch').value;
    if (input.value !== value) input.value = value;
    const buttons = toolbar.querySelectorAll('.fseg button');
    incoming.querySelectorAll('.fseg button').forEach((button,i) => { buttons[i].setAttribute('aria-pressed',button.getAttribute('aria-pressed')); buttons[i].innerHTML=button.innerHTML; });
    replaceAround(m,next,toolbar,incoming);
  } else m.replaceChildren(...next.childNodes);
  m.dataset.view = viewKey;
  if (typeof BridgeSettings !== 'undefined') BridgeSettings.activate();
  if (typeof BridgeBackup !== 'undefined') BridgeBackup.activate({mount:S.view === 'settings' ? document.getElementById('backupSettingsMount') : null,enabled:feat('backup') === true});
  if (keepFocus && id && !ae.isConnected && (ae.tagName !== 'SELECT' || selectKeyboardFocus)) { const el = document.getElementById(id); if (el) { el.focus({preventScroll:true}); try { el.setSelectionRange(...selection); } catch (e) {} } }
}

function renderBusy() {
  const b = $('#busyRoot');
  if (!S.radio.label || S.radio.quiet) { b.innerHTML = ''; return; }
  const q = S.radio.queue.length;
  b.innerHTML = `<div class="busybar" role="status"><span class="pulse"></span><div class="p"><b>Bluetooth busy · ${esc(S.radio.label)}</b>${S.radio.step ? ` <span style="opacity:.75">· ${esc(S.radio.step)}</span>` : ''}</div>
    <span class="small" style="opacity:.75">${q ? `${q} more waiting` : 'Other changes wait their turn'}</span></div>`;
}
let dragging = false, activeNativeSelect = null, selectKeyboardFocus = false, composingField = null, pressedButton = null;
function nativeSelect(target) { return target?.closest?.('#drawerRoot select, #main select, #modalRoot select'); }
function releaseNativeSelect() {
  if (!activeNativeSelect) return;
  activeNativeSelect = null; renderSoon();
}
// A native dropdown is destroyed when its DOM node is replaced. Snapshots can
// update the model while it is open, but its containing form waits for dismissal.
document.addEventListener('focusin', e => {
  const select = nativeSelect(e.target);
  if (select) activeNativeSelect = select;
  else if (!['BODY','HTML'].includes(e.target?.tagName)) releaseNativeSelect();
});
document.addEventListener('pointerdown', e => {
  selectKeyboardFocus = false;
  const select = nativeSelect(e.target);
  if (select) activeNativeSelect = select; else releaseNativeSelect();
});
document.addEventListener('change', e => { if (e.target === activeNativeSelect) releaseNativeSelect(); });
document.addEventListener('focusout', e => {
  // A system picker can take focus out of the page while it is still open.
  // Pointer pickers release on change, outside click or a real focus transfer.
  if (e.target === activeNativeSelect && (selectKeyboardFocus ||
      e.relatedTarget && !['BODY','HTML'].includes(e.relatedTarget.tagName))) releaseNativeSelect();
});
document.addEventListener('keydown', e => {
  selectKeyboardFocus = true;
  if (activeNativeSelect && e.key === 'Escape') {
    const select = activeNativeSelect; releaseNativeSelect();
    if (document.activeElement === select && !select.closest('#modalRoot')) select.blur();
  }
});
document.addEventListener('pointerdown', e => { if (e.target.matches('input[type=range]')) dragging = true; });
document.addEventListener('pointerup', () => { if (dragging || pressedButton) { dragging = false; pressedButton = null; renderSoon(); } });
document.addEventListener('pointerdown', e => { pressedButton = e.target.closest?.('button') || null; });
function releasePointer() { dragging = false; pressedButton = null; renderSoon(); }
document.addEventListener('pointercancel', releasePointer);
window.addEventListener('blur', releasePointer);
document.addEventListener('compositionstart', e => { if (textEntry(e.target)) composingField = e.target; });
document.addEventListener('compositionend', () => { composingField = null; renderSoon(); });
document.addEventListener('focusout', e => { if (textEntry(e.target)) renderSoon(); });
function render(preserveText = false) {
  if (dragging || pressedButton?.isConnected) return renderSoon();
  syncDialogLayers();
  const ds = $('#drawerRoot'), scroll = ds.querySelector('.drawer-body')?.scrollTop || 0;
  const ae = document.activeElement, focusId = ae && ae.closest && ae.closest('#drawerRoot') && ae.id;
  updateSceneProgress();
  updateAccessorySetupPrompts(); // Status must advance even while an open field is preserved.
  if (composingField?.isConnected) return;
  if (activeNativeSelect?.isConnected) {
    renderNav();
    if (!activeNativeSelect.closest('#main')) renderMain(true,preserveText);
    renderBusy(); return;
  }
  if (S.drawer?.kind === 'scene') {
    renderNav(); renderMain(true,preserveText); renderBusy(); renderSceneDrawer(ds); syncDialogLayers(); return;
  }
  if (focusId && (ae.tagName === 'INPUT' && ae.type === 'text' || S.drawer?.kind === 'scene' && textEntry(ae))) { renderNav(); renderMain(true,preserveText); renderBusy(); return; } // don't wipe a name being typed
  renderNav(); renderMain(true,preserveText); renderBusy();
  ds.innerHTML = drawerHtml();
  syncDialogLayers();
  const nb = ds.querySelector('.drawer-body'); if (nb) nb.scrollTop = scroll;
  // Refocusing a replacement select can reopen the native mobile picker.
  // Preserve focus only for keyboard navigation after a committed selection.
  if (focusId && (ae.tagName !== 'SELECT' || selectKeyboardFocus)) document.getElementById(focusId)?.focus();
}
let soon = null;
function renderSoon() { clearTimeout(soon); soon = setTimeout(() => { if (dragging || pressedButton?.isConnected) return renderSoon(); render(true); }, 120); }

/* ---------- live data ---------- */
/* The stream sends changes immediately at its next tick and refreshes idle clients periodically. */
let es = null; const lastStates = {};
let snapshotGeneration = 0, refreshRequest = 0;
function applySnapshot(d) {
  snapshotGeneration++;
  const st = d.status || {}, inv = d.inventory || {devices:[], groups:[]}, states = d.device_states || {};
  if (st.instance && lastCapabilityInstance && lastCapabilityInstance !== st.instance) {
    S.caps = {features:{}, types:{}};
    capabilityEpoch++;
    capabilityLoad = null;
  }
  if (st.instance) lastCapabilityInstance = st.instance;
  if (!S.caps.types?.controllers || !S.caps.types?.lights) {
    waitingSnapshot = d;
    S.status = st;
    S.synced = false;
    S.devices = [];
    S.groups = [];
    S.revision = null;
    snapshotAt = 0;
    updateDimmerControlReview();
    loadCapabilities();
    renderSoon();
    return;
  }
  snapshotAt = Date.now(); S.groupWorkflow = d.group_workflow || null; S.controllerViews = d.controllers || [];
  if (d.scene_workflow) { sceneAccept(d.scene_workflow); sceneWatchRunning(); }
  const discoveryEpoch = wizardDiscoveryEpoch();
  S.status = st; S.revision = st.inventory_revision; S.roomMap = d.device_rooms || {};
  if (S.wiz?.step === 1 && discoveryEpoch !== wizardDiscoveryEpoch() && Object.keys(S.wiz.found).length) {
    Object.values(S.wiz.found).forEach(row => { row.sel = false; });
    S.wiz.searchComplete = false; S.wiz.error = 'The bridge connection changed. Search again before pairing.';
    renderSoon();
  }
  S.health = {mqtt_enabled: !!st.mqtt_enabled, mqtt_connected: !!st.mqtt_ready};
  const ble = st.mesh_ready ? 4 : st.bluetooth_startup_retrying ? 6 : st.bluetooth_starting ? 2 : 5;
  if (S.synced && ble !== S.ble && (ble === 4 || (S.ble === 4 && ble === 5)))
    log('system', st.mesh_ready ? 'Mesh connected' : 'Mesh disconnected');
  S.ble = ble; S.rx = st.receive_count || 0;
  const t = S.caps.types || {}, ctrls = t.controllers || {}, names = t.lights || {}, color = t.color_temp || [93, 134, 137, 162];
  const groupsOf = {};
  (inv.groups || []).forEach(g => (g.members || []).forEach(m => (groupsOf[m] = groupsOf[m] || []).push(g.group_id)));
  S.devices = (inv.devices || []).map(x => {
    const current = states[String(x.device_id)] || {}, fallback = d.last_known_device_states?.[String(x.device_id)] || {};
    const s = Object.fromEntries(Object.entries(fallback).map(([field, reading]) => [field, {...reading, fresh:false}]));
    if (current.state || current.brightness) { delete s.state; delete s.brightness; }
    Object.assign(s, current);
    const isCtrl = ctrls[x.product_id] !== undefined;
    const out = {avion_id:x.device_id, name:x.name, product_type:x.product_id, exposed:x.exposed === true,
      product_name:names[x.product_id] || ctrls[x.product_id], kind:isCtrl ? 'controller' : 'light',
      has_color_temp:color.includes(x.product_id), has_dimming:x.product_id !== 167,
      groups:groupsOf[x.device_id] || [], room:S.roomMap[String(x.device_id)] || ''};
    if (s.state && s.state.value === 'OFF') out.brightness = 0;
    else if (s.brightness) out.brightness = s.brightness.value;
    else if (s.state && s.state.value === 'ON') out.brightness = 255;
    out.power_read_status = d.power_read_status ? d.power_read_status[x.device_id] || 'unchecked' : undefined;
    const firstStatus = d.initial_light_status?.[String(x.device_id)]?.remaining_ms;
    out.initial_status_until = Number.isFinite(firstStatus) && firstStatus > 0 ? Date.now() + Math.min(firstStatus, 120000) : null;
    out.power_last_known = !current.state && !current.brightness && !!(s.state || s.brightness);
    out.color_temp_last_known = !current.color_temp && !!s.color_temp;
    out.brightness_fresh = s.state?.value === 'OFF' ? s.state.fresh === true : s.brightness?.fresh === true;
    out.power_fresh = s.state?.fresh === true || out.brightness_fresh;
    out.power_observed_ms = s.state?.observed_ms ?? s.brightness?.observed_ms;
    if (s.color_temp) out.color_temp = s.color_temp.value;
    out.color_temp_fresh = s.color_temp?.fresh === true;
    const ages = ['state', 'brightness', 'color_temp'].filter(k => s[k]).map(k => s[k].age_ms);
    if (ages.length) reportedAt[x.device_id] = Date.now() - Math.min(...ages); else delete reportedAt[x.device_id];
    const key = ['state', 'brightness', 'color_temp'].map(k => s[k] ? s[k].observed_ms : '').join();
    if (S.synced && !out.power_last_known && lastStates[x.device_id] !== undefined && lastStates[x.device_id] !== key && known(out))
      log('light', `${x.name} reported ${out.brightness ? pct(out.brightness) + '%' : 'off'}${out.color_temp ? ', ' + out.color_temp + ' K' : ''}`);
    lastStates[x.device_id] = key;
    return out;
  });
  S.groups = (inv.groups || []).map(g => ({group_id:g.group_id, name:g.name, members:(g.members || []).slice(), exposed:g.exposed === true, protected:!!g.protected}));
  settleAccessoryRequests();
  updateDimmerControlReview();
  settleBatterySetups();
  settleGroupPowerIntents();
  settleDevicePowerIntents();
  settleRoomPowerIntents();
  if (!S.synced) log('system', 'Connected to the bridge', `${lights().length} lights, ${controllers().length} controllers, ${lightingGroups().length} groups`);
  S.synced = true;
  // The stream repeats the whole snapshot every second. Re-render only when something shown changed,
  // so open menus, focus and clicks are not interrupted.
  const sig = JSON.stringify([S.devices, S.groups, S.roomMap, st.pairing, st.mesh_ready, st.bluetooth_starting, st.bluetooth_startup_retrying, st.pending_transactions, st.publication_pending, S.view === 'settings' ? st.command_timings : null, st.last_error, st.mqtt_enabled, st.mqtt_ready, st.startup_read, S.groupWorkflow, S.controllerViews, S.sceneWorkflow, sceneEpoch, st.jobs, st.keypad_saves, st.keypad_progress, st.anyplace_progress, st.claim_progress, S.view === 'add' && S.wiz?.step === 1 ? wizRows().map(rowSeenState) : null]);
  if (sig !== lastSig) { lastSig = sig; renderSoon(); }
}
let lastSig = '', lastCapabilityInstance = null, waitingSnapshot = null;
let capabilityLoad = null, capabilityRetry = null, capabilityEpoch = 0;
function loadCapabilities() {
  if (capabilityLoad || capabilityRetry) return capabilityLoad;
  const epoch = capabilityEpoch;
  capabilityLoad = (async () => {
    try {
      const caps = await getJson('/api/capabilities');
      if (epoch !== capabilityEpoch) return;
      if (!caps?.types?.lights || !caps.types.controllers ||
          (lastCapabilityInstance && caps.instance && caps.instance !== lastCapabilityInstance)) throw new Error('Device types pending');
      S.caps = caps;
      if (caps.instance) lastCapabilityInstance = caps.instance;
      if ((S.view === 'add' && !feat('pair_lights')) || (S.view === 'scenes' && !feat('scenes'))) S.view = 'home';
      const snapshot = waitingSnapshot;
      waitingSnapshot = null;
      if (snapshot) applySnapshot(snapshot);
      renderSoon();
    } catch (e) {
      if (epoch === capabilityEpoch) capabilityRetry = setTimeout(() => { capabilityRetry = null; loadCapabilities(); }, 2000);
    } finally {
      if (epoch === capabilityEpoch) capabilityLoad = null;
    }
  })();
  return capabilityLoad;
}
async function refreshSnapshot(strict = false) {
  const generation = snapshotGeneration, request = ++refreshRequest;
  try {
    const value = await getJson('/api/snapshot');
    if (request === refreshRequest && generation === snapshotGeneration) applySnapshot(value);
  } catch (e) { if (strict) throw e; }
}
let esRetry = null;
function connect() {
  clearTimeout(esRetry); esRetry = null;
  if (es) es.close();
  const stream = es = new EventSource('/api/events');
  es.onopen = () => { if (stream === es) { S.sse = true; renderSoon(); } };
  es.onerror = () => {
    if (stream !== es) return;
    S.sse = false; renderSoon();
    // The browser reconnects a dropped stream by itself, but not one the bridge refused
    // (for example while too many pages are open). Ask again after a pause.
    if (stream.readyState === 2 && !esRetry) esRetry = setTimeout(connect, 5000);
  };
  es.addEventListener('snapshot', e => { if (stream !== es) return; try { S.sse = true; applySnapshot(JSON.parse(e.data)); } catch (err) {} });
}
async function start() {
  try { const v = localStorage.getItem('halo-view'); if (v && NAV.some(n => n[0] === v)) S.view = v; } catch (e) {}
  await loadCapabilities();
  render(); await refreshSnapshot(); if (S.view === 'scenes') await refreshScenes(); connect();
  // Keep "· N min ago" current without disturbing an open drawer, dialog or field.
  setInterval(() => { const a = document.activeElement;
    if (!S.drawer && !$('#modalRoot').innerHTML && !(a && /^(SELECT|INPUT|TEXTAREA)$/.test(a.tagName))) renderSoon(); }, 60000);
  // A phone can drop the live stream while the page is in the background without reporting an error.
  setInterval(() => { if (!document.hidden && S.synced && Date.now() - snapshotAt > 10000) connect(); }, 5000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && S.synced && Date.now() - snapshotAt > 3000) { refreshSnapshot(); connect(); }
  });
}
start();
