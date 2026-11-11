// ComfyUI-Vast-Worker sidebar: run target switch, instance status, API keys,
// GPU settings, the remote model library, and the confirmation dialogs.
//
// Everything is rendered with textContent, never innerHTML, because several
// of the strings shown here come from Vast.ai or from the rented instance.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const TAB_ID = "vast-worker";
const state = { status: null, settings: null, models: [], folders: [], offers: null, keyInfo: null, seenEvent: 0 };
const ui = {};
let dialogEl = null;
let dialogId = null;

// ---------------------------------------------------------------- helpers
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style") el.style.cssText = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key in el && key !== "list") el[key] = value;
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function call(path, body) {
  const options = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const resp = await api.fetchApi(`/vast_worker${path}`, options);
  let data = {};
  try { data = await resp.json(); } catch (_) { /* empty body */ }
  if (!resp.ok) throw new Error(data.error || `Request failed (${resp.status})`);
  return data;
}

function toast(severity, summary, detail) {
  try {
    app.extensionManager.toast.add({ severity, summary, detail, life: severity === "error" ? 9000 : 4500 });
  } catch (_) {
    console.log(`[vast-worker] ${summary}: ${detail || ""}`);
  }
}

async function guarded(fn) {
  try { return await fn(); } catch (err) { toast("error", "Vast Worker", err.message); }
}

const money = (n) => (n === null || n === undefined ? "?" : `$${Number(n).toFixed(Number(n) < 1 ? 3 : 2)}`);
function duration(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
function bytes(n) {
  if (!n && n !== 0) return "?";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0; let v = Number(n);
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}

const PHASE_TEXT = {
  none: "No instance",
  searching: "Searching offers",
  confirming: "Waiting for confirmation",
  renting: "Renting",
  booting: "Starting",
  installing: "Setting up",
  ready: "Ready",
  destroying: "Destroying",
  error: "Needs attention",
};

// ----------------------------------------------------------------- styles
const CSS = `
.vw-root{padding:12px;display:flex;flex-direction:column;gap:14px;font-size:13px;color:var(--fg-color,#ddd);overflow-y:auto;height:100%;box-sizing:border-box}
.vw-root h3{margin:0;font-size:15px;font-weight:600}
.vw-root h4{margin:0 0 6px;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--descrip-text,#999)}
.vw-card{border:1px solid var(--border-color,#444);border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:8px;background:var(--comfy-menu-secondary-bg,rgba(127,127,127,.06))}
.vw-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.vw-grow{flex:1 1 auto;min-width:0}
.vw-muted{color:var(--descrip-text,#999)}
.vw-small{font-size:11.5px}
.vw-break{overflow-wrap:anywhere}
.vw-seg{display:grid;grid-template-columns:1fr 1fr;border:1px solid var(--border-color,#444);border-radius:8px;overflow:hidden}
.vw-seg button{border:0;border-radius:0;padding:8px 6px;background:transparent;color:inherit;cursor:pointer;font:inherit}
.vw-seg button.vw-on{background:var(--p-primary-color,#3b82f6);color:#fff;font-weight:600}
.vw-seg button.vw-on.vw-vast{background:#d97706}
.vw-btn{border:1px solid var(--border-color,#555);background:var(--comfy-input-bg,#2a2a2a);color:var(--input-text,#ddd);border-radius:6px;padding:5px 10px;cursor:pointer;font:inherit}
.vw-btn:hover{filter:brightness(1.15)}
.vw-btn:disabled{opacity:.5;cursor:default}
.vw-btn.vw-primary{background:var(--p-primary-color,#3b82f6);border-color:transparent;color:#fff}
.vw-btn.vw-danger{background:#b91c1c;border-color:transparent;color:#fff}
.vw-input{width:100%;box-sizing:border-box;border:1px solid var(--border-color,#555);background:var(--comfy-input-bg,#2a2a2a);color:var(--input-text,#ddd);border-radius:6px;padding:5px 7px;font:inherit}
.vw-field{display:flex;flex-direction:column;gap:3px;flex:1 1 120px;min-width:0}
.vw-field>span{font-size:11.5px;color:var(--descrip-text,#999)}
.vw-card>.vw-field{flex:0 0 auto}
.vw-picker{display:flex;flex-direction:column;gap:5px}
.vw-chips{display:flex;flex-wrap:wrap;gap:4px;align-items:center;min-height:22px}
.vw-chip{display:inline-flex;align-items:center;gap:2px;padding:2px 4px 2px 9px;border-radius:999px;background:rgba(127,127,127,.24);font-size:12px}
.vw-chip button{border:0;background:transparent;color:inherit;cursor:pointer;font-size:15px;line-height:1;padding:0 4px;opacity:.65}
.vw-chip button:hover{opacity:1}
.vw-badge{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:2px 9px;font-size:11.5px;font-weight:600;background:rgba(127,127,127,.2)}
.vw-badge::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.vw-badge.vw-ready{color:#22c55e}.vw-badge.vw-work{color:#f59e0b}.vw-badge.vw-error{color:#ef4444}.vw-badge.vw-none{color:var(--descrip-text,#999)}
.vw-kv{display:grid;grid-template-columns:auto 1fr;gap:2px 10px}
.vw-kv dt{color:var(--descrip-text,#999)}.vw-kv dd{margin:0;text-align:right;overflow-wrap:anywhere}
.vw-bar{height:5px;border-radius:3px;background:rgba(127,127,127,.25);overflow:hidden}
.vw-bar>i{display:block;height:100%;background:#d97706;transition:width .3s}
.vw-list{display:flex;flex-direction:column;gap:6px;margin:0;padding:0;list-style:none}
.vw-list li{display:flex;gap:8px;align-items:flex-start;border-top:1px solid var(--border-color,#333);padding-top:6px}
.vw-list li:first-child{border-top:0;padding-top:0}
.vw-log{max-height:150px;overflow-y:auto;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;display:flex;flex-direction:column;gap:2px}
.vw-log .vw-warn{color:#f59e0b}.vw-log .vw-err{color:#ef4444}
.vw-warnbox{border-color:#b45309;background:rgba(217,119,6,.12)}
.vw-pill{position:fixed;left:50%;transform:translateX(-50%);bottom:12px;z-index:900;display:none;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;background:#d97706;color:#fff;font-size:12px;font-weight:600;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.35)}
.vw-pill::before{content:"";width:8px;height:8px;border-radius:50%;background:#fff}
.vw-overlay{position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;padding:16px}
.vw-dialog{width:min(560px,100%);max-height:90vh;overflow-y:auto;background:var(--comfy-menu-bg,#202020);color:var(--fg-color,#ddd);border:1px solid var(--border-color,#555);border-radius:10px;padding:18px;display:flex;flex-direction:column;gap:12px;font-size:13px}
.vw-dialog h3{margin:0;font-size:16px}
.vw-offer{display:flex;gap:10px;align-items:center;border:1px solid var(--border-color,#444);border-radius:8px;padding:8px 10px;cursor:pointer}
.vw-offer.vw-picked{border-color:#d97706;background:rgba(217,119,6,.12)}
.vw-offer b{font-weight:600}
.vw-right{margin-left:auto;text-align:right;white-space:nowrap}
.vw-browser{width:min(1120px,100%);height:min(86vh,900px);background:var(--comfy-menu-bg,#202020);color:var(--fg-color,#ddd);border:1px solid var(--border-color,#555);border-radius:10px;display:flex;flex-direction:column;font-size:13px;overflow:hidden}
.vw-browser header{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border-color,#444)}
.vw-browser header h3{margin:0;font-size:16px;flex:1}
.vw-filters{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;padding:10px 16px;border-bottom:1px solid var(--border-color,#444)}
.vw-filters .vw-field{flex:0 0 auto}
.vw-filters .vw-input{width:auto;min-width:92px}
.vw-drop{position:relative}
.vw-drop>button{min-width:150px;max-width:240px;text-align:left;display:flex;gap:8px;align-items:center;justify-content:space-between}
.vw-drop>button span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vw-pop{position:absolute;top:calc(100% + 4px);left:0;z-index:5;width:330px;max-height:420px;overflow-y:auto;background:var(--comfy-menu-bg,#202020);border:1px solid var(--border-color,#555);border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:6px;box-shadow:0 8px 24px rgba(0,0,0,.45)}
.vw-pop .vw-opt{display:flex;gap:8px;align-items:center;padding:3px 2px;cursor:pointer}
.vw-pop .vw-opt.vw-sub{padding-left:24px}
.vw-pop .vw-opt .vw-right{font-size:11.5px;color:var(--descrip-text,#999)}
.vw-pop .vw-opt.vw-off{opacity:.45;cursor:default}
.vw-caret{border:0;background:transparent;color:inherit;cursor:pointer;padding:0 4px;margin-left:auto}
.vw-results{flex:1;overflow-y:auto;padding:6px 16px 12px}
.vw-result{display:grid;grid-template-columns:minmax(150px,1.5fr) minmax(110px,1fr) minmax(120px,1fr) minmax(150px,1.3fr) 70px 100px auto;gap:12px;align-items:center;padding:10px 4px;border-bottom:1px solid var(--border-color,#333)}
.vw-result .vw-big{font-size:15px;font-weight:600}
.vw-result .vw-price{font-size:16px;font-weight:700;text-align:right;white-space:nowrap}
.vw-result.vw-head{position:sticky;top:0;background:var(--comfy-menu-bg,#202020);font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--descrip-text,#999);padding:8px 4px;z-index:1}
.vw-browser footer{display:flex;gap:10px;align-items:center;padding:10px 16px;border-top:1px solid var(--border-color,#444)}
.vw-tag{display:inline-block;border-radius:4px;padding:0 5px;font-size:10.5px;background:rgba(59,130,246,.25);margin-left:6px;vertical-align:middle}
@media (max-width:900px){.vw-result{grid-template-columns:1fr 1fr auto}.vw-result>.vw-hide{display:none}}
`;

// ------------------------------------------------------------- data layer
async function refresh() {
  const [status, models] = await Promise.all([call("/status"), call("/models")]);
  state.status = status;
  state.statusAt = performance.now();
  state.settings = status.settings;
  state.models = models.models;
  state.folders = models.folders;
  render();
  renderModels();
  if (ui.root && !ui.settingsShown) {  // do not overwrite a form the user is editing
    renderSettings();
    ui.settingsShown = true;
  }
  for (const pending of status.confirmations || []) showConfirm(pending);
}

function onStatus(snapshot) {
  const previous = state.status;
  state.status = { ...(previous || {}), ...snapshot };
  state.statusAt = performance.now();
  for (const event of snapshot.events || []) {
    if (event.time > state.seenEvent) {
      if (state.seenEvent && event.level === "error") toast("error", "Vast Worker", event.message);
      state.seenEvent = event.time;
    }
  }
  if (previous && previous.phase !== snapshot.phase) {
    if (snapshot.phase === "ready") toast("success", "Vast.ai instance ready", snapshot.message);
    if (previous.phase === "destroying" && snapshot.phase === "none") toast("info", "Vast.ai instance destroyed", "Billing has stopped.");
  }
  render();
  if (browser.el) renderResults();
}

async function setMode(mode) {
  await guarded(async () => {
    const snapshot = await call("/mode", { mode });
    onStatus(snapshot);
    // Loader dropdowns list the remote model library only in Vast mode.
    try { await app.refreshComboInNodes?.(); } catch (_) { /* older frontends */ }
  });
}

// -------------------------------------------------------------- rendering
function render() {
  const st = state.status;
  if (!st) return;
  renderPill(st);
  if (!ui.root) return;

  ui.local.classList.toggle("vw-on", st.mode === "local");
  ui.vast.classList.toggle("vw-on", st.mode === "vast");
  ui.modeHint.textContent = st.mode === "vast"
    ? "Run sends the whole workflow to a rented Vast.ai GPU. Models are downloaded there, not here."
    : "Run uses this computer. Nothing is rented.";

  // instance card
  const phaseClass = st.phase === "ready" ? "vw-ready" : st.phase === "error" ? "vw-error" : st.phase === "none" ? "vw-none" : "vw-work";
  ui.badge.className = `vw-badge ${phaseClass}`;
  ui.badge.textContent = PHASE_TEXT[st.phase] || st.phase;
  ui.message.textContent = st.message || "";
  ui.message.style.display = st.message ? "" : "none";
  const inst = st.instance;
  ui.clocks = {};
  ui.instanceInfo.replaceChildren();
  if (inst) {
    const rows = [
      ["GPU", `${inst.num_gpus > 1 ? inst.num_gpus + "× " : ""}${inst.gpu_name || "?"}${inst.gpu_ram_gb ? ` · ${inst.gpu_ram_gb} GB` : ""}`],
      ["Price", `${money(inst.dph_total)}/hour`],
      ["Running for", duration(inst.uptime_seconds), "uptime"],
      ["Spent so far (estimate)", money(inst.cost_estimate), "cost"],
      ["Location", inst.geolocation || "?"],
      ["Instance", `#${inst.id}`],
    ];
    if (inst.disk_free_bytes !== null && inst.disk_free_bytes !== undefined) {
      rows.splice(4, 0, ["Disk free", `${bytes(inst.disk_free_bytes)}${inst.disk_gb ? ` of ${inst.disk_gb} GB` : ""}`]);
    }
    if (st.phase === "ready" && !st.busy) {
      rows.splice(4, 0, ["Auto-destroy in", duration(inst.idle_limit_seconds - inst.idle_seconds), "idle"]);
    }
    for (const [k, v, clock] of rows) {
      const dd = h("dd", {}, v);
      if (clock) ui.clocks[clock] = dd;
      ui.instanceInfo.append(h("dt", {}, k), dd);
    }
  }
  ui.instanceInfo.style.display = inst ? "" : "none";
  ui.destroy.style.display = inst ? "" : "none";
  ui.start.style.display = !inst && ["none", "error"].includes(st.phase) ? "" : "none";
  ui.start.disabled = !st.secrets?.vast_api_key;
  ui.browse.style.display = ui.start.style.display;
  ui.browse.disabled = ui.start.disabled;

  // activity
  const act = st.activity;
  ui.activity.replaceChildren();
  ui.activityCard.style.display = act ? "" : "none";
  if (act) {
    ui.activity.append(h("div", { class: "vw-break" }, act.message || act.phase));
    if (act.job_seconds !== null && act.job_seconds !== undefined) {
      ui.clocks.job = h("div", { class: "vw-small vw-muted" }, `Job running for ${duration(act.job_seconds)}`);
      ui.activity.append(ui.clocks.job);
    }
    for (const item of act.items || []) {
      const pct = item.total ? Math.min(100, (100 * (item.bytes || 0)) / item.total) : (["done", "exists"].includes(item.state) ? 100 : 0);
      // Installs have no byte count; show the step they are on and for how long.
      const detail = item.total && item.state !== "queued" ? `${bytes(item.bytes)} / ${bytes(item.total)}`
        : item.step ? `${item.step} · ${duration(item.step_seconds)}` : item.state;
      ui.activity.append(
        h("div", { class: "vw-small vw-row" }, h("span", { class: "vw-grow vw-break" }, item.name), h("span", { class: "vw-muted" }, item.error ? "failed" : detail)),
        h("div", { class: "vw-bar" }, h("i", { style: `width:${pct}%` })),
      );
    }
    if (act.detail) ui.activity.append(h("div", { class: "vw-log vw-muted vw-break", title: "Latest line from the instance" }, act.detail));
  }

  // leftovers
  ui.othersCard.style.display = (st.others || []).length ? "" : "none";
  ui.others.replaceChildren(...(st.others || []).map((o) => h("li", {},
    h("div", { class: "vw-grow vw-break" }, `#${o.id} · ${o.gpu_name || "?"} · ${money(o.dph_total)}/h · ${o.actual_status || "?"}`),
    h("button", { class: "vw-btn vw-danger", onclick: () => guarded(async () => {
      if (!(await ask(`Destroy instance #${o.id}?`, "Everything on it is deleted and its billing stops. This cannot be undone.", "Destroy"))) return;
      onStatus(await call("/destroy_other", { id: o.id }));
    }) }, "Destroy"))));

  // keys
  for (const [name, chip] of Object.entries(ui.keyChips)) {
    const where = st.secrets?.[name];
    chip.textContent = where === "stored" ? "saved" : where === "env" ? "from environment" : "not set";
    chip.className = `vw-badge ${where ? "vw-ready" : "vw-none"}`;
  }
  ui.keyInfo.textContent = state.keyInfo || "";

  // log
  ui.log.replaceChildren(...(st.events || []).slice(-40).reverse().map((e) => h("div",
    { class: e.level === "error" ? "vw-err" : e.level === "warn" ? "vw-warn" : "" },
    `${new Date(e.time * 1000).toLocaleTimeString()}  ${e.message}`)));
  renderClocks();
}

// The server pushes a snapshot when something changes and otherwise only every
// 20 s, which is too coarse while a job runs. The clocks are advanced here
// from the time the last snapshot arrived, once a second.
function renderClocks() {
  const st = state.status;
  const clocks = ui.clocks;
  if (!st || !clocks) return;
  const elapsed = state.statusAt ? (performance.now() - state.statusAt) / 1000 : 0;
  const inst = st.instance;
  if (inst) {
    const uptime = inst.uptime_seconds + elapsed;
    if (clocks.uptime) clocks.uptime.textContent = duration(uptime);
    if (clocks.cost) clocks.cost.textContent = money((inst.dph_total * uptime) / 3600);
    if (clocks.idle) clocks.idle.textContent = duration(inst.idle_limit_seconds - inst.idle_seconds - elapsed);
  }
  const job = st.activity?.job_seconds;
  if (clocks.job && job !== null && job !== undefined) clocks.job.textContent = `Job running for ${duration(job + elapsed)}`;
}

function renderPill(st) {
  if (!ui.pill) return;
  const show = st.mode === "vast" || !!st.instance;
  ui.pill.style.display = show ? "inline-flex" : "none";
  if (!show) return;
  let text = st.mode === "vast" ? "Run → Vast.ai" : "Vast.ai instance running";
  if (st.instance) text += ` · ${PHASE_TEXT[st.phase] || st.phase} · ${money(st.instance.dph_total)}/h`;
  ui.pill.textContent = text;
  ui.pill.style.background = st.mode === "vast" ? "#d97706" : "#6b7280";
}

function renderSettings() {
  const s = state.settings;
  if (!s || !ui.fields) return;
  ui.fields.min_vram_gb.value = s.gpu.min_vram_gb;
  ui.fields.max_dph.value = s.gpu.max_dph;
  ui.pickers.gpu_names.set(s.gpu.gpu_names);
  ui.pickers.countries.set(s.gpu.countries);
  ui.pickers.exclude_countries.set(s.gpu.exclude_countries);
  ui.fields.min_reliability.value = s.gpu.min_reliability;
  ui.fields.min_inet_down_mbps.value = s.gpu.min_inet_down_mbps;
  ui.fields.disk_gb.value = s.instance.disk_gb;
  ui.fields.image.value = s.instance.image;
  ui.fields.idle_minutes.value = s.lifecycle.idle_minutes;
  ui.fields.confirm_rental.checked = !!s.lifecycle.confirm_rental;
  ui.fields.scheme.value = s.connection.scheme;
  ui.fields.local_models.value = s.upload.local_models;
  ui.fields.confirm_over_mb.value = s.upload.confirm_over_mb;
  ui.fields.civitai_lookup.checked = !!s.upload.civitai_lookup;
}

// ------------------------------------------------- GPU and country pickers
let regionNames = null;
try { regionNames = new Intl.DisplayNames(["en"], { type: "region" }); } catch (_) { /* older browsers */ }
function countryName(code) {
  try { return regionNames?.of(code) || code; } catch (_) { return code; }
}

// A list of values shown as removable chips. Suggestions come from what is
// on offer at Vast.ai right now; anything else can still be typed.
function chipPicker({ placeholder, emptyText, label, parse }) {
  let values = [];
  let options = [];
  const listId = `vw-options-${Math.random().toString(36).slice(2)}`;
  const chips = h("div", { class: "vw-chips" });
  const datalist = h("datalist", { id: listId });
  const input = h("input", { class: "vw-input", placeholder, list: listId, autocomplete: "off" });

  const draw = () => {
    chips.replaceChildren(...values.map((value) => h("span", { class: "vw-chip" }, label(value),
      h("button", { type: "button", title: "Remove", onclick: () => { values = values.filter((v) => v !== value); draw(); } }, "×"))));
    if (!values.length) chips.append(h("span", { class: "vw-small vw-muted" }, emptyText));
  };
  const add = () => {
    for (const raw of input.value.split(",")) {
      const value = parse(raw.trim(), options);
      if (value && !values.includes(value)) values.push(value);
    }
    input.value = "";
    draw();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); }
  });
  input.addEventListener("change", add);  // leaving the field with text in it
  input.addEventListener("input", (e) => {  // picking a suggestion from the list
    if ((!e.inputType || e.inputType === "insertReplacementText") && options.some((o) => o.value === input.value)) add();
  });
  input.addEventListener("focus", () => loadMarket());
  draw();
  return {
    el: h("div", { class: "vw-picker" }, chips, input, datalist),
    get: () => [...values],
    set: (next) => { values = [...(next || [])]; draw(); },
    setOptions: (next) => {
      options = next;
      datalist.replaceChildren(...next.map((o) => h("option", { value: o.value, label: o.label })));
    },
  };
}

async function loadMarket() {
  if (state.market || state.marketLoading || !state.status?.secrets?.vast_api_key) return;
  state.marketLoading = true;
  try {
    state.market = await call("/market");
    applyMarket();
  } catch (_) {
    // Offline or the key was rejected: typing values by hand still works.
  } finally {
    state.marketLoading = false;
  }
}

function applyMarket() {
  const market = state.market;
  if (!market || !ui.pickers) return;
  ui.pickers.gpu_names.setOptions(market.gpus.map((g) => ({
    value: g.name,
    label: `${g.vram_gb} GB · from ${money(g.min_dph)}/h · ${g.offers} offer${g.offers === 1 ? "" : "s"}`
      + (g.compute_cap && g.compute_cap < 750 ? " · too old for the image" : ""),
  })));
  const countries = market.countries.map((c) => ({
    value: c.code,
    name: countryName(c.code),
    label: `${countryName(c.code)} · ${c.offers} offer${c.offers === 1 ? "" : "s"} · from ${money(c.min_dph)}/h`,
  }));
  ui.pickers.countries.setOptions(countries);
  ui.pickers.exclude_countries.setOptions(countries);
}

function parseGpu(text, options) {
  if (!text) return null;
  const known = options.find((o) => o.value.toLowerCase() === text.toLowerCase());
  return known ? known.value : text;
}

function parseCountry(text, options) {
  if (!text) return null;
  if (/^[A-Za-z]{2}$/.test(text)) return text.toUpperCase();
  const known = options.find((o) => o.name.toLowerCase() === text.toLowerCase());
  if (known) return known.value;
  toast("warn", "Vast Worker", `"${text}" is not a country I know. Use a two-letter code such as JP or US.`);
  return null;
}

function renderModels() {
  if (!ui.models) return;
  ui.models.replaceChildren(...state.models.map((m) => h("li", {},
    h("div", { class: "vw-grow vw-break" }, h("div", {}, m.name),
      h("div", { class: "vw-small vw-muted" }, `${m.directory}${m.source === "workflow" ? " · from a workflow" : m.source === "civitai" ? " · found on Civitai" : ""}`)),
    h("button", { class: "vw-btn", title: "Remove from the library", onclick: () => guarded(async () => {
      state.models = (await call("/models/remove", { directory: m.directory, name: m.name })).models;
      renderModels();
    }) }, "Remove"))));
  if (!state.models.length) ui.models.append(h("li", { class: "vw-muted" }, "No models yet. Add a download link below."));
  const current = ui.modelFolder.value;
  ui.modelFolder.replaceChildren(...state.folders.map((f) => h("option", { value: f }, f)));
  ui.modelFolder.value = state.folders.includes(current) ? current : (state.folders.includes("checkpoints") ? "checkpoints" : state.folders[0] || "");
}

function renderOffers() {
  if (!ui.offers) return;
  const offers = state.offers;
  ui.offers.replaceChildren();
  if (offers === null) return;
  if (!offers.length) {
    ui.offers.append(h("li", { class: "vw-muted" }, "No offers match. Raise the price limit or lower the requirements."));
    return;
  }
  for (const o of offers) {
    ui.offers.append(h("li", {},
      h("div", { class: "vw-grow vw-break" }, `${o.gpu_name} · ${o.gpu_ram_gb ?? "?"} GB`,
        h("div", { class: "vw-small vw-muted" }, `${o.geolocation || "?"} · ${Math.round(o.inet_down || 0)} Mbps down · reliability ${(100 * (o.reliability || 0)).toFixed(1)}%`)),
      h("div", {}, `${money(o.dph_total)}/h`)));
  }
}

// ---------------------------------------------------------------- sidebar
function field(label, input) {
  return h("label", { class: "vw-field" }, h("span", {}, label), input);
}

// Like field(), for a control made of several elements (a <label> would send
// clicks on its text to the first button inside).
function block(label, content) {
  return h("div", { class: "vw-field" }, h("span", {}, label), content);
}

function buildSidebar(el) {
  ui.local = h("button", { onclick: () => setMode("local") }, "This computer");
  ui.vast = h("button", { class: "vw-vast", onclick: () => setMode("vast") }, "Vast.ai");
  ui.modeHint = h("div", { class: "vw-small vw-muted" });

  ui.badge = h("span", { class: "vw-badge vw-none" }, "No instance");
  ui.message = h("div", { class: "vw-small vw-break" });
  ui.instanceInfo = h("dl", { class: "vw-kv vw-small" });
  ui.start = h("button", { class: "vw-btn", title: "Rent and boot a machine now, so the first run does not wait",
    onclick: () => guarded(() => call("/start", {})) }, "Start cheapest match");
  ui.browse = h("button", { class: "vw-btn vw-primary", title: "Search Vast.ai offers and rent the machine you pick",
    onclick: () => openBrowser() }, "Browse machines");
  ui.destroy = h("button", { class: "vw-btn vw-danger", onclick: () => guarded(async () => {
    if (!(await ask("Destroy the Vast.ai instance?",
      "Everything on it, including downloaded models, is deleted and billing stops. The next run on Vast.ai rents a new machine.", "Destroy"))) return;
    ui.destroy.disabled = true;
    ui.destroy.textContent = "Destroying…";
    try {
      onStatus(await call("/destroy", {}));
    } finally {
      ui.destroy.disabled = false;
      ui.destroy.textContent = "Destroy instance";
    }
  }) }, "Destroy instance");

  ui.activity = h("div", { style: "display:flex;flex-direction:column;gap:5px" });
  ui.activityCard = h("div", { class: "vw-card" }, h("h4", {}, "Current job"), ui.activity);

  ui.others = h("ul", { class: "vw-list vw-small" });
  ui.othersCard = h("div", { class: "vw-card vw-warnbox" }, h("h4", {}, "Other instances still billing"),
    h("div", { class: "vw-small" }, "These were rented by this plugin but are not tracked by this session."), ui.others);

  // API keys
  ui.keyChips = {};
  const keyRow = (name, label, placeholder) => {
    const input = h("input", { class: "vw-input", type: "password", placeholder, autocomplete: "off" });
    ui.keyChips[name] = h("span", { class: "vw-badge vw-none" }, "not set");
    const save = h("button", { class: "vw-btn", onclick: () => guarded(async () => {
      await call("/secrets", { [name]: input.value });
      input.value = "";
      await refresh();
      toast("success", "Vast Worker", `${label} ${state.status.secrets[name] ? "saved" : "removed"}.`);
    }) }, "Save");
    return h("div", { style: "display:flex;flex-direction:column;gap:4px" },
      h("div", { class: "vw-row" }, h("span", { class: "vw-grow" }, label), ui.keyChips[name]),
      h("div", { class: "vw-row" }, h("div", { class: "vw-grow" }, input), save));
  };
  ui.keyInfo = h("div", { class: "vw-small vw-muted" });
  const verify = h("button", { class: "vw-btn", onclick: () => guarded(async () => {
    const info = await call("/verify_key", {});
    state.keyInfo = `Key works. Credit: ${info.credit === null || info.credit === undefined ? "unknown" : money(info.credit)}`;
    render();
  }) }, "Check Vast.ai key");

  // settings
  const num = (step) => h("input", { class: "vw-input", type: "number", step, min: "0" });
  ui.fields = {
    min_vram_gb: num("1"), max_dph: num("0.01"),
    min_reliability: num("0.01"), min_inet_down_mbps: num("50"),
    disk_gb: num("10"), image: h("input", { class: "vw-input", placeholder: "auto" }), idle_minutes: num("1"),
    confirm_rental: h("input", { type: "checkbox" }),
    scheme: h("select", { class: "vw-input" }, h("option", { value: "https" }, "HTTPS (encrypted)"), h("option", { value: "http" }, "HTTP")),
    local_models: h("select", { class: "vw-input" },
      h("option", { value: "auto" }, "Upload it automatically"),
      h("option", { value: "ask" }, "Ask me first"),
      h("option", { value: "off" }, "Never upload — I will give a link")),
    confirm_over_mb: num("100"),
    civitai_lookup: h("input", { type: "checkbox" }),
  };
  ui.pickers = {
    gpu_names: chipPicker({ placeholder: "add a model or part of a name, e.g. 4060", emptyText: "Any GPU model",
      label: (v) => v, parse: parseGpu }),
    countries: chipPicker({ placeholder: "add a country, e.g. JP", emptyText: "Any country",
      label: (v) => `${countryName(v)} (${v})`, parse: parseCountry }),
    exclude_countries: chipPicker({ placeholder: "add a country to avoid", emptyText: "None",
      label: (v) => `${countryName(v)} (${v})`, parse: parseCountry }),
  };
  const save = async () => {
    const f = ui.fields;
    const p = ui.pickers;
    const result = await call("/settings", {
      gpu: { min_vram_gb: f.min_vram_gb.value, max_dph: f.max_dph.value, gpu_names: p.gpu_names.get(),
        countries: p.countries.get(), exclude_countries: p.exclude_countries.get(),
        min_reliability: f.min_reliability.value, min_inet_down_mbps: f.min_inet_down_mbps.value },
      instance: { disk_gb: f.disk_gb.value, image: f.image.value || "auto" },
      lifecycle: { idle_minutes: f.idle_minutes.value, confirm_rental: f.confirm_rental.checked },
      connection: { scheme: f.scheme.value },
      upload: { local_models: f.local_models.value, confirm_over_mb: f.confirm_over_mb.value,
        civitai_lookup: f.civitai_lookup.checked },
    });
    state.settings = result.settings;
    renderSettings();
  };
  const saveButton = () => h("button", { class: "vw-btn vw-primary", onclick: () => guarded(async () => {
    await save();
    toast("success", "Vast Worker", "Settings saved.");
  }) }, "Save settings");
  ui.offers = h("ul", { class: "vw-list vw-small" });
  const preview = h("button", { class: "vw-btn", title: "Saves the conditions above, then searches Vast.ai",
    onclick: () => guarded(async () => {
      await save();
      state.offers = (await call("/offers", {})).offers;
      renderOffers();
    }) }, "Show matching offers");

  // model library
  ui.models = h("ul", { class: "vw-list vw-small" });
  ui.modelUrl = h("input", { class: "vw-input", placeholder: "https://huggingface.co/…/resolve/main/model.safetensors" });
  ui.modelFolder = h("select", { class: "vw-input" });
  ui.modelName = h("input", { class: "vw-input", placeholder: "file name (optional for Hugging Face links)" });
  const addModel = h("button", { class: "vw-btn vw-primary", onclick: () => guarded(async () => {
    const result = await call("/models", { url: ui.modelUrl.value, directory: ui.modelFolder.value, name: ui.modelName.value });
    state.models = result.models;
    ui.modelUrl.value = ""; ui.modelName.value = "";
    renderModels();
    try { await app.refreshComboInNodes?.(); } catch (_) { /* older frontends */ }
    toast("success", "Vast Worker", `${result.entry.name} added. It is downloaded on the instance when a workflow uses it.`);
  }) }, "Add to library");

  ui.log = h("div", { class: "vw-log" });

  ui.root = h("div", { class: "vw-root" },
    h("div", {}, h("h3", {}, "Vast Worker"), h("div", { class: "vw-small vw-muted" }, "Run workflows on a rented Vast.ai GPU")),
    h("div", { class: "vw-card" }, h("h4", {}, "Run on"), h("div", { class: "vw-seg" }, ui.local, ui.vast), ui.modeHint),
    h("div", { class: "vw-card" }, h("div", { class: "vw-row" }, h("h4", { class: "vw-grow", style: "margin:0" }, "Instance"), ui.badge),
      ui.message, ui.instanceInfo, h("div", { class: "vw-row" }, ui.browse, ui.start, ui.destroy)),
    ui.activityCard,
    ui.othersCard,
    h("div", { class: "vw-card" }, h("h4", {}, "Access keys"),
      keyRow("vast_api_key", "Vast.ai API key", "paste a key from cloud.vast.ai → Keys"),
      keyRow("hf_token", "Hugging Face token (optional)", "for gated or private models"),
      keyRow("civitai_token", "Civitai token (optional)", "for Civitai downloads"),
      h("div", { class: "vw-row" }, verify, ui.keyInfo),
      h("div", { class: "vw-small vw-muted" }, "Keys are stored on this computer only and are never shown again or saved into workflows.")),
    h("div", { class: "vw-card" }, h("h4", {}, "Search conditions"),
      h("div", { class: "vw-small vw-muted" }, "Which machines may be rented. The cheapest match is offered first."),
      h("div", { class: "vw-row" }, field("Price limit ($/hour)", ui.fields.max_dph), field("Minimum VRAM (GB)", ui.fields.min_vram_gb)),
      block("GPU models", ui.pickers.gpu_names.el),
      h("div", { class: "vw-small vw-muted" }, "Part of a name is enough: 4060 matches RTX 4060 and RTX 4060 Ti."),
      block("Only in these countries", ui.pickers.countries.el),
      block("Never in these countries", ui.pickers.exclude_countries.el),
      h("div", { class: "vw-row" }, field("Minimum reliability (0–1)", ui.fields.min_reliability), field("Minimum download (Mbps)", ui.fields.min_inet_down_mbps)),
      h("div", { class: "vw-row" }, saveButton(), preview,
        h("button", { class: "vw-btn", onclick: () => openBrowser() }, "Browse machines")), ui.offers),
    h("div", { class: "vw-card" }, h("h4", {}, "Behaviour"),
      h("label", { class: "vw-row" }, ui.fields.confirm_rental, h("span", {}, "Ask me before renting a machine")),
      field("Destroy the instance after idle (minutes)", ui.fields.idle_minutes),
      h("label", { class: "vw-row" }, ui.fields.civitai_lookup,
        h("span", {}, "Look for my local models on Civitai first, and download them there instead of uploading")),
      field("A local model that cannot be downloaded", ui.fields.local_models),
      field("Ask before uploading a file larger than (MB, 0 = never ask)", ui.fields.confirm_over_mb),
      h("div", { class: "vw-small vw-muted" }, "Models are downloaded on the instance whenever a link is known or Civitai recognises the file (only the file's hash is sent). Uploading is the fallback, also when a download fails. Reference images and other input files are always uploaded."),
      h("div", {}, saveButton())),
    h("div", { class: "vw-card" }, h("h4", {}, "Machine setup"),
      h("div", { class: "vw-row" }, field("Disk (GB)", ui.fields.disk_gb), field("Docker image", ui.fields.image)),
      h("div", { class: "vw-small vw-muted" }, "ComfyUI itself takes about 10 GB of the disk; the rest is for your models and custom nodes. The size cannot be changed after renting."),
      field("Connection to the instance", ui.fields.scheme),
      h("div", {}, saveButton())),
    h("div", { class: "vw-card" }, h("h4", {}, "Remote model library"),
      h("div", { class: "vw-small vw-muted" }, "Models listed here are downloaded on the instance when a workflow uses them. In Vast mode they also appear in loader dropdowns."),
      ui.models, field("Download link", ui.modelUrl),
      h("div", { class: "vw-row" }, field("Folder", ui.modelFolder), field("File name", ui.modelName)), h("div", {}, addModel)),
    h("div", { class: "vw-card" }, h("h4", {}, "Activity log"), ui.log),
  );
  el.replaceChildren(ui.root);
  ui.settingsShown = !!state.settings;
  render(); renderSettings(); renderModels(); renderOffers();
  applyMarket();
  loadMarket();
}

// ---------------------------------------------------------------- dialogs
function closeDialog() {
  dialogEl?.remove();
  dialogEl = null;
  dialogId = null;
}

// A yes/no question in the same style as the other dialogs.
function ask(title, text, okLabel, okClass = "vw-danger") {
  return new Promise((resolve) => {
    const overlay = h("div", { class: "vw-overlay" });
    const done = (value) => { overlay.remove(); resolve(value); };
    overlay.append(h("div", { class: "vw-dialog", role: "dialog", "aria-modal": "true" },
      h("h3", {}, title), h("div", { class: "vw-break" }, text),
      h("div", { class: "vw-row", style: "justify-content:flex-end" },
        h("button", { class: "vw-btn", onclick: () => done(false) }, "Cancel"),
        h("button", { class: `vw-btn ${okClass}`, onclick: () => done(true) }, okLabel))));
    document.body.append(overlay);
  });
}

async function answer(id, body) {
  closeDialog();
  await guarded(() => call("/confirm", { id, ...body }));
}

function showConfirm(payload) {
  if (!payload || dialogId === payload.id) return;
  closeDialog();
  dialogId = payload.id;
  let content;
  if (payload.kind === "rent") {
    let picked = payload.offers[0]?.id;
    const rows = payload.offers.map((o) => {
      const row = h("div", { class: "vw-offer", onclick: () => {
        picked = o.id;
        rows.forEach((r) => r.classList.toggle("vw-picked", r === row));
      } },
      h("div", {}, h("b", {}, `${o.num_gpus > 1 ? o.num_gpus + "× " : ""}${o.gpu_name}`), ` · ${o.gpu_ram_gb ?? "?"} GB VRAM`,
        h("div", { class: "vw-small vw-muted" }, `${o.geolocation || "?"} · ${Math.round(o.inet_down || 0)} Mbps down · reliability ${(100 * (o.reliability || 0)).toFixed(1)}%`)),
      h("div", { class: "vw-right" }, h("b", {}, `${money(o.dph_total)}/hour`)));
      return row;
    });
    rows[0]?.classList.add("vw-picked");
    content = [
      h("h3", {}, "Rent a Vast.ai GPU?"),
      h("div", {}, "This workflow is set to run on Vast.ai and no instance is running. Pick a machine:"),
      ...rows,
      h("div", { class: "vw-small vw-muted vw-break" },
        `Billing starts when you confirm and stops when the instance is destroyed — automatically after ${payload.idle_minutes} idle minutes, or from the sidebar. `
        + `Disk: ${payload.disk_gb} GB. Image: ${payload.image}. Starting a new machine usually takes a few minutes.`),
      h("div", { class: "vw-row", style: "justify-content:flex-end" },
        h("button", { class: "vw-btn", onclick: () => answer(payload.id, { approved: false }) }, "Cancel"),
        h("button", { class: "vw-btn vw-primary", onclick: () => answer(payload.id, { approved: true, offer_id: picked }) }, "Rent and run")),
    ];
  } else if (payload.kind === "models") {
    // A model that is on this computer can be uploaded instead of linked.
    const inputs = payload.models.map((m) => {
      const local = m.local_size !== null && m.local_size !== undefined;
      return {
        model: m,
        local,
        upload: local ? h("input", { type: "checkbox", checked: true }) : null,
        url: h("input", { class: "vw-input", placeholder: local ? "…or paste a download link to use instead" : "https://… download link" }),
      };
    });
    const anyLocal = inputs.some((i) => i.local);
    content = [
      h("h3", {}, anyLocal ? "These models are not on the instance yet" : "Where can these models be downloaded?"),
      h("div", {}, anyLocal
        ? "A file on this computer can be uploaded from here. A download link is usually faster for large files, and is remembered in the remote model library."
        : "The instance does not have these files and no download link is known for them. Links are remembered in the remote model library."),
      ...inputs.map(({ model, local, upload, url }) => h("div", { class: "vw-card" },
        h("div", { class: "vw-break" }, h("b", {}, model.name)),
        h("div", { class: "vw-small vw-muted" }, `Folder: ${model.directory} · used by ${model.node}`),
        local ? h("label", { class: "vw-row" }, upload, h("span", {}, `Upload my copy from this computer (${bytes(model.local_size)})`)) : null,
        url)),
      h("div", { class: "vw-row", style: "justify-content:flex-end" },
        h("button", { class: "vw-btn", onclick: () => answer(payload.id, { approved: false }) }, "Cancel run"),
        h("button", { class: "vw-btn vw-primary", onclick: () => {
          const models = [];
          for (const { model, upload, url } of inputs) {
            const link = url.value.trim();
            if (link) {
              if (!/^https?:\/\//.test(link)) { toast("warn", "Vast Worker", "A download link must start with http:// or https://"); return; }
              models.push({ name: model.name, directory: model.directory, url: link });
            } else if (upload?.checked) {
              models.push({ name: model.name, directory: model.directory, upload: true });
            } else {
              toast("warn", "Vast Worker", `${model.name} needs a download link${upload ? " or the upload box ticked" : ""}.`);
              return;
            }
          }
          answer(payload.id, { approved: true, models });
        } }, "Continue")),
    ];
  } else {
    return;
  }
  dialogEl = h("div", { class: "vw-overlay" }, h("div", { class: "vw-dialog", role: "dialog", "aria-modal": "true" }, ...content));
  document.body.append(dialogEl);
}

// ------------------------------------------------------- machine browser
// A searchable list of Vast.ai offers. Filters start from the saved search
// conditions; a machine can be rented straight from its row.
const browser = { el: null, filters: null, sort: "price", offers: [], total: 0, timer: null, seq: 0 };
const GPU_GROUPS = ["RTX", "GTX", "Titan", "Quadro", "Datacenter and other"];

function gpuGroup(name) {
  if (/^RTX/i.test(name)) return "RTX";
  if (/^GTX/i.test(name)) return "GTX";
  if (/^Titan/i.test(name)) return "Titan";
  if (/^(Quadro|Q )/i.test(name)) return "Quadro";
  return "Datacenter and other";
}

function closeBrowser() {
  clearTimeout(browser.timer);
  browser.el?.remove();
  browser.el = null;
}

function scheduleSearch(delay = 350) {
  clearTimeout(browser.timer);
  browser.timer = setTimeout(runSearch, delay);
}

async function runSearch() {
  if (!browser.el) return;
  const seq = ++browser.seq;
  browser.status.textContent = "Searching Vast.ai…";
  try {
    const f = browser.filters;
    const data = await call("/offers", {
      gpu: { gpu_names: f.gpu_names, countries: f.countries, max_dph: f.max_dph, min_vram_gb: f.min_vram_gb },
      sort: browser.sort, limit: 100,
    });
    if (seq !== browser.seq || !browser.el) return;  // a newer search has started
    browser.offers = data.offers;
    browser.total = data.total;
    renderResults();
  } catch (err) {
    if (seq === browser.seq && browser.el) {
      browser.offers = [];
      browser.status.textContent = err.message;
      renderResults();
    }
  }
}

function renderResults() {
  const hasInstance = !!state.status?.instance;
  const rows = browser.offers.map((o) => h("div", { class: "vw-result" },
    h("div", {}, h("div", { class: "vw-big" }, `${o.num_gpus || 1}× ${o.gpu_name}`),
      h("div", { class: "vw-small vw-muted" }, `${o.gpu_ram_gb ?? "?"} GB VRAM · CUDA ${o.cuda_max_good ?? "?"}`)),
    h("div", { class: "vw-hide" }, o.geolocation || "?", o.datacenter ? h("span", { class: "vw-tag" }, "datacenter") : null,
      h("div", { class: "vw-small vw-muted" }, `${(100 * (o.reliability || 0)).toFixed(1)}% reliable`)),
    h("div", { class: "vw-hide" }, `↑${Math.round(o.inet_up || 0)} ↓${Math.round(o.inet_down || 0)} Mbps`,
      h("div", { class: "vw-small vw-muted" }, `${money(o.inet_down_cost || 0)}/GB download`)),
    h("div", { class: "vw-hide" }, `${Math.round(o.cpu_cores || 0)} CPU cores · ${Math.round(o.cpu_ram_gb || 0)} GB RAM`,
      h("div", { class: "vw-small vw-muted" }, `${Math.round(o.disk_space || 0)} GB disk available`)),
    h("div", { class: "vw-hide" }, o.dlperf ? Number(o.dlperf).toFixed(1) : "–"),
    h("div", { class: "vw-price" }, `${money(o.dph_total)}/hr`),
    h("button", { class: "vw-btn vw-primary", disabled: hasInstance, onclick: () => rentOffer(o) }, "Rent")));
  browser.results.replaceChildren(
    h("div", { class: "vw-result vw-head" }, h("div", {}, "GPU"), h("div", { class: "vw-hide" }, "Location"),
      h("div", { class: "vw-hide" }, "Network"), h("div", { class: "vw-hide" }, "Host"),
      h("div", { class: "vw-hide" }, "DLPerf"), h("div", { style: "text-align:right" }, "Price"), h("div", {})),
    ...rows);
  if (!rows.length) browser.results.append(h("div", { class: "vw-muted", style: "padding:24px 4px" },
    "No machine matches these filters. Raise the price limit, lower the VRAM, or clear a filter."));
  if (browser.status.textContent === "Searching Vast.ai…" || rows.length) {
    browser.status.textContent = hasInstance
      ? "An instance is already running. Destroy it in the sidebar before renting another machine."
      : `${browser.total} machine${browser.total === 1 ? "" : "s"} can run the ComfyUI image and match${browser.total === 1 ? "es" : ""}`
        + (browser.total > rows.length ? ` · showing the first ${rows.length}` : "");
  }
}

async function rentOffer(offer) {
  const idle = state.settings?.lifecycle?.idle_minutes ?? 15;
  const ok = await ask(`Rent ${offer.gpu_name} at ${money(offer.dph_total)}/hour?`,
    `${offer.geolocation || ""}. Billing starts now and stops when the instance is destroyed: automatically after ${idle} idle minutes, or from the sidebar. `
    + `Disk: ${state.settings?.instance?.disk_gb ?? "?"} GB, of which about 10 GB is used by ComfyUI itself. `
    + "Starting a new machine usually takes a few minutes.", "Rent", "vw-primary");
  if (!ok) return;
  await guarded(async () => {
    await call("/start", { offer_id: offer.id });
    closeBrowser();
    toast("info", "Vast Worker", `Starting ${offer.gpu_name}. Progress is shown in the sidebar.`);
  });
}

// A button with a popover of checkboxes, like the filter menus on vast.ai.
function dropdown(label, renderBody) {
  const text = h("span", {}, label);
  const button = h("button", { class: "vw-btn", type: "button" }, text, "▾");
  const pop = h("div", { class: "vw-pop", style: "display:none" });
  const el = h("div", { class: "vw-drop" }, button, pop);
  const api = {
    el, pop,
    setLabel: (value) => { text.textContent = value; },
    refresh: () => { if (pop.style.display !== "none") renderBody(pop, api); },
    close: () => { pop.style.display = "none"; },
  };
  button.addEventListener("click", () => {
    const open = pop.style.display === "none";
    browser.dropdowns.forEach((d) => d.close());
    if (open) { pop.style.display = "flex"; renderBody(pop, api); }
  });
  return api;
}

function option(checked, label, detail, onChange, extra = {}) {
  const box = h("input", { type: "checkbox", checked, disabled: !!extra.disabled, indeterminate: !!extra.indeterminate });
  box.addEventListener("change", () => onChange(box.checked));
  return h("label", { class: `vw-opt ${extra.sub ? "vw-sub" : ""} ${extra.disabled ? "vw-off" : ""}` },
    box, h("span", { class: "vw-grow" }, label), detail ? h("span", { class: "vw-right" }, detail) : null, extra.after || null);
}

function gpuLabel() {
  const names = browser.filters.gpu_names;
  return !names.length ? "Any GPU" : names.length === 1 ? names[0] : `${names.length} GPU filters`;
}

function regionLabel() {
  const codes = browser.filters.countries;
  return !codes.length ? "All regions" : codes.length === 1 ? countryName(codes[0]) : `${codes.length} countries`;
}

function renderGpuMenu(pop, api) {
  const f = browser.filters;
  const gpus = state.market?.gpus || [];
  const known = new Set(gpus.map((g) => g.name));
  const query = (browser.gpuQuery || "").trim().toLowerCase();
  const squash = (s) => s.toLowerCase().replace(/\s+/g, "");
  const shown = gpus.filter((g) => !query || squash(g.name).includes(squash(query)));
  const changed = () => { api.setLabel(gpuLabel()); renderGpuMenu(pop, api); scheduleSearch(); };
  const toggle = (name, on) => {
    f.gpu_names = on ? [...new Set([...f.gpu_names, name])] : f.gpu_names.filter((n) => n !== name);
    changed();
  };

  const search = h("input", { class: "vw-input", placeholder: "Search, e.g. 4060", value: browser.gpuQuery || "" });
  search.addEventListener("input", () => {
    browser.gpuQuery = search.value;
    renderGpuMenu(pop, api);
    const again = pop.querySelector("input.vw-input");
    again.focus();
    again.setSelectionRange(again.value.length, again.value.length);
  });
  const body = [h("div", { class: "vw-row" }, h("b", { class: "vw-grow" }, "GPU name"),
    h("button", { class: "vw-btn", onclick: () => { f.gpu_names = []; browser.gpuQuery = ""; changed(); } }, "Reset")), search];

  // Typed fragments that are not a full model name ("4060") stay as partial matches.
  const partial = f.gpu_names.filter((n) => !known.has(n));
  if (partial.length) {
    body.push(h("div", { class: "vw-chips" }, ...partial.map((p) => h("span", { class: "vw-chip" }, `contains “${p}”`,
      h("button", { type: "button", title: "Remove", onclick: () => toggle(p, false) }, "×")))));
  }
  if (query && shown.length > 1 && !known.has(browser.gpuQuery.trim())) {
    body.push(h("button", { class: "vw-btn", onclick: () => { toggle(browser.gpuQuery.trim(), true); } },
      `Match every model containing “${browser.gpuQuery.trim()}” (${shown.length})`));
  }
  if (!gpus.length) body.push(h("div", { class: "vw-muted vw-small" }, "Loading the list of GPU models…"));

  browser.openGroups = browser.openGroups || new Set();
  for (const group of GPU_GROUPS) {
    const members = shown.filter((g) => gpuGroup(g.name) === group);
    if (!members.length) continue;
    const usable = members.filter((g) => !(g.compute_cap && g.compute_cap < 750));
    const picked = usable.filter((g) => f.gpu_names.includes(g.name));
    const open = !!query || browser.openGroups.has(group);
    const caret = h("button", { class: "vw-caret", type: "button", title: open ? "Collapse" : "Expand",
      onclick: (e) => { e.preventDefault(); open ? browser.openGroups.delete(group) : browser.openGroups.add(group); renderGpuMenu(pop, api); } },
    open ? "▴" : "▾");
    body.push(option(usable.length > 0 && picked.length === usable.length, group, `${members.length} models`, (on) => {
      const names = new Set(usable.map((g) => g.name));
      f.gpu_names = on ? [...new Set([...f.gpu_names, ...names])] : f.gpu_names.filter((n) => !names.has(n));
      changed();
    }, { indeterminate: picked.length > 0 && picked.length < usable.length, after: caret, disabled: !usable.length }));
    if (open) {
      for (const g of members) {
        const tooOld = g.compute_cap && g.compute_cap < 750;
        body.push(option(f.gpu_names.includes(g.name), g.name,
          tooOld ? "too old for the image" : `${g.vram_gb} GB · from ${money(g.min_dph)}/h · ${g.offers}`,
          (on) => toggle(g.name, on), { sub: true, disabled: tooOld }));
      }
    }
  }
  pop.replaceChildren(...body);
}

function renderRegionMenu(pop, api) {
  const f = browser.filters;
  const query = (browser.regionQuery || "").trim().toLowerCase();
  const blocked = new Set(state.settings?.gpu?.exclude_countries || []);
  const countries = (state.market?.countries || []).filter((c) => !blocked.has(c.code))
    .filter((c) => !query || countryName(c.code).toLowerCase().includes(query) || c.code.toLowerCase() === query);
  const changed = () => { api.setLabel(regionLabel()); renderRegionMenu(pop, api); scheduleSearch(); };
  const search = h("input", { class: "vw-input", placeholder: "Search countries", value: browser.regionQuery || "" });
  search.addEventListener("input", () => {
    browser.regionQuery = search.value;
    renderRegionMenu(pop, api);
    const again = pop.querySelector("input.vw-input");
    again.focus();
    again.setSelectionRange(again.value.length, again.value.length);
  });
  const body = [h("div", { class: "vw-row" }, h("b", { class: "vw-grow" }, "Country"),
    h("button", { class: "vw-btn", onclick: () => { f.countries = []; browser.regionQuery = ""; changed(); } }, "Reset")), search];
  if (!state.market) body.push(h("div", { class: "vw-muted vw-small" }, "Loading the list of countries…"));
  for (const c of countries) {
    body.push(option(f.countries.includes(c.code), `${countryName(c.code)} (${c.code})`, `${c.offers} · from ${money(c.min_dph)}/h`, (on) => {
      f.countries = on ? [...new Set([...f.countries, c.code])] : f.countries.filter((x) => x !== c.code);
      changed();
    }));
  }
  if (blocked.size) {
    body.push(h("div", { class: "vw-small vw-muted" },
      `Never rented in: ${[...blocked].map((c) => countryName(c)).join(", ")} (change this in the sidebar).`));
  }
  pop.replaceChildren(...body);
}

function openBrowser() {
  if (browser.el || !state.settings) return;
  const s = state.settings;
  browser.filters = { gpu_names: [...s.gpu.gpu_names], countries: [...s.gpu.countries], max_dph: s.gpu.max_dph, min_vram_gb: s.gpu.min_vram_gb };
  browser.sort = "price";
  browser.gpuQuery = "";
  browser.regionQuery = "";

  const gpuMenu = dropdown(gpuLabel(), renderGpuMenu);
  const regionMenu = dropdown(regionLabel(), renderRegionMenu);
  browser.dropdowns = [gpuMenu, regionMenu];
  const price = h("input", { class: "vw-input", type: "number", step: "0.01", min: "0.01", value: browser.filters.max_dph });
  price.addEventListener("input", () => { browser.filters.max_dph = Number(price.value) || 0.01; scheduleSearch(600); });
  const vram = h("input", { class: "vw-input", type: "number", step: "1", min: "0", value: browser.filters.min_vram_gb });
  vram.addEventListener("input", () => { browser.filters.min_vram_gb = Number(vram.value) || 0; scheduleSearch(600); });
  const sort = h("select", { class: "vw-input" }, h("option", { value: "price" }, "Price: low to high"),
    h("option", { value: "performance" }, "Performance (DLPerf)"), h("option", { value: "value" }, "Performance per dollar"));
  sort.addEventListener("change", () => { browser.sort = sort.value; scheduleSearch(0); });

  browser.results = h("div", { class: "vw-results" });
  browser.status = h("div", { class: "vw-grow vw-muted" }, "Searching Vast.ai…");
  const saveFilters = h("button", { class: "vw-btn", title: "Automatic renting when you press Run will use these filters",
    onclick: () => guarded(async () => {
      const f = browser.filters;
      const result = await call("/settings", { gpu: { gpu_names: f.gpu_names, countries: f.countries, max_dph: f.max_dph, min_vram_gb: f.min_vram_gb } });
      state.settings = result.settings;
      renderSettings();
      toast("success", "Vast Worker", "These filters are now your search conditions.");
    }) }, "Save as my search conditions");

  const panel = h("div", { class: "vw-browser", role: "dialog", "aria-modal": "true" },
    h("header", {}, h("h3", {}, "Find a machine on Vast.ai"),
      h("button", { class: "vw-btn", onclick: closeBrowser }, "Close")),
    h("div", { class: "vw-filters" },
      block("GPU", gpuMenu.el), block("Region", regionMenu.el),
      field("Max price ($/hour)", price), field("Min VRAM (GB)", vram), field("Sort by", sort)),
    browser.results,
    h("footer", {}, browser.status, saveFilters));
  browser.el = h("div", { class: "vw-overlay" }, panel);
  browser.el.addEventListener("mousedown", (e) => {
    if (e.target === browser.el) closeBrowser();
    else if (!e.target.closest(".vw-drop")) browser.dropdowns.forEach((d) => d.close());
  });
  document.body.append(browser.el);
  renderResults();
  loadMarket().then(() => browser.dropdowns?.forEach((d) => d.refresh()));
  runSearch();
}

// ------------------------------------------------------------ registration
app.registerExtension({
  name: "vast-worker.sidebar",
  async setup() {
    document.head.append(h("style", {}, CSS));
    ui.pill = h("div", { class: "vw-pill", title: "Open the Vast Worker panel", onclick: () => {
      try { app.extensionManager.sidebarTab.toggleSidebarTab(TAB_ID); } catch (_) { /* older frontends */ }
    } });
    document.body.append(ui.pill);

    api.addEventListener("vast_worker.status", (e) => onStatus(e.detail));
    setInterval(renderClocks, 1000);
    api.addEventListener("vast_worker.confirm", (e) => showConfirm(e.detail));
    api.addEventListener("vast_worker.confirm_closed", (e) => {
      if (dialogId === e.detail?.id) closeDialog();
      guarded(refresh);  // an answered dialog may have added models to the library
    });

    app.extensionManager.registerSidebarTab({
      id: TAB_ID,
      icon: "icon-[lucide--cloud]",
      title: "Vast Worker",
      tooltip: "Vast Worker — run on a rented Vast.ai GPU",
      type: "custom",
      render: (el) => {
        buildSidebar(el);
        guarded(refresh);
      },
    });
    await guarded(refresh);
  },
});
