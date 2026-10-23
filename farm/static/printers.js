// every request carries X-Farm: a page on another site can't add a custom header without a CORS preflight, which
// Farm Control never answers - so it can't drive the API (approve, apply, ...) from someone's browser
const _farmFetch = window.fetch.bind(window);
window.fetch = (u, o = {}) => _farmFetch(u, { ...o, headers: { ...(o.headers || {}), "X-Farm": "1" } });
"use strict";
// Printers tab: fleet health, farm-baseline audit, config snapshots and drift. Read-only towards the printers;
// changes to printers are staged and applied through the gate; onboarding writes printers/registry.json.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// a value as a JS literal inside an HTML attribute (instance keys and paths come from the printers)
const js = (v) => esc(JSON.stringify(v));
const ls = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } };
const ago = (t) => { if (!t) return "never"; const s = Date.now() / 1000 - t; return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`; };
let data = null, fd = { key: null, files: [], path: null, mode: "diff" };

const operator = () => farmOperator();     // static/operator.js: the "You" link in the header

async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operator: operator(), ...(body || {}) }) });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) { alert(out.error || `HTTP ${r.status}`); return null; }
  return out;
}

function serviceChips(svcs, hostName) {
  const any = (data.instances || []).find((v) => v.host === hostName && v.reachable);
  const entries = Object.entries(svcs || {}).filter(([n]) => !/backup-on-boot|backup-filewatch/.test(n));
  const bad = entries.filter(([, s]) => !s.startsWith("active/running"));
  const chip = ([n, s]) => `<span class="chip ${s.startsWith("active/running") ? "ok" : /auto-restart|failed/.test(s) ? "bad" : "warn"}" title="${esc(s)}">${esc(n)}${s.startsWith("active/running") ? "" : ": " + esc(s.split("/")[1])}
      ${any ? `<a href="#" class="lnk" title="restart ${esc(n)}" onclick='restartSvc(${js(any.key)}, ${js(n)});return false'>↻</a>` : ""}</span>`;
  return (bad.length ? bad.map(chip).join(" ") : `<span class="chip ok">all services running</span>`) +
    ` <details style="display:inline-block"><summary>all ${entries.length}</summary>${entries.map(chip).join(" ")}</details>`;
}

async function stageFix(key, rule) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  const r = await post(`/api/fleet/${encodeURIComponent(key)}/fix`, { rule });
  if (!r) return;
  alert(`Staged ${r.length} change(s): ${r.map((c) => c.id + " " + c.path + " (" + c.summary + ")").join(", ")}. Review the diff on the card, then Apply when ${key} is idle.`);
  refresh();
}
async function stageAllFixes(key, n) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  if (!confirm(`Stage the generated fixes for ${n} failing rules on ${key}? Nothing reaches the printer until you Apply.`)) return;
  const r = await post(`/api/fleet/${encodeURIComponent(key)}/fix-all`);
  if (r) alert(`Staged ${r.length} change(s): ${r.map((c) => c.id + " " + c.path).join(", ")}. Review the diff on the card, then Apply when ${key} is idle.`);
  refresh();
}
async function telegramPack(key) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  if (!confirm(`Apply the farm Telegram pack to ${key}'s bot? [bot] (token, chat) is kept; a copy of the old telegram.conf stays on the printer; only the bot restarts.`)) return;
  const r = await post(`/api/fleet/${encodeURIComponent(key)}/telegram-pack`);
  if (r) alert(`${r.service}: ${r.state}${r.rolled_back ? " - did not start, old telegram.conf restored" : ""}. Replaced: ${r.replaced.join(", ")}. Backup: ${r.backup}`);
  refresh();
}
async function restartSvc(key, service) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  if (!confirm(`Restart ${service}? (Refused if it would disturb a print.)`)) return;
  const r = await post(`/api/fleet/${encodeURIComponent(key)}/restart`, { service });
  if (r) alert(`${r.service}: ${r.state}`);
  refresh();
}
async function loadUsb(host, el) {
  el.innerHTML = "reading…";
  const r = await (await fetch(`/api/hosts/${encodeURIComponent(host)}/usb`)).json();
  if (r.error) { el.innerHTML = `<span class="warn">${esc(r.error)}</span>`; return; }
  const row = (d) => `<tr><td>${esc(d.path.replace("/dev/serial/by-path/", "").replace("/dev/v4l/by-id/", ""))}</td><td class="dim">${esc(d.target)}</td>
      <td class="${d.used_by ? "" : "warn"}">${esc(d.used_by || "not used by any printer config")}</td></tr>`;
  el.innerHTML = `<table><tr><th colspan=3>Serial (printer boards)</th></tr>${r.serial.map(row).join("")}<tr><th colspan=3>Cameras</th></tr>${r.cameras.map(row).join("")}</table>`;
}
async function makeScript(form) {
  const f = Object.fromEntries(new FormData(form).entries());
  if (f.klipper_commit === "") delete f.klipper_commit;
  const r = await fetch("/api/scripts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(f) });
  const t = await r.text();
  form.querySelector("pre").textContent = r.ok ? t : (JSON.parse(t).error || t);
}
function hostTools(h) {
  if (!h.ssh) return "";
  return `<details><summary>USB devices &amp; host scripts</summary>
    <div class="row" style="margin:6px 0"><button class="ghost" onclick="loadUsb('${esc(h.name)}', this.parentNode.nextElementSibling)">Read USB devices (ssh, read-only)</button></div><div></div>
    <form onsubmit="makeScript(this);return false" class="row" style="margin-top:8px"><b>Camera service</b>
      <input type="hidden" name="kind" value="camera"><input name="name" placeholder="printer name" style="width:110px">
      <input name="device" placeholder="by-id ...-video-index0" style="width:300px"><input name="port" placeholder="8083" style="width:70px">
      <button class="ghost">Make script</button><pre style="width:100%;max-height:30vh;overflow:auto"></pre></form>
    <form onsubmit="makeScript(this);return false" class="row"><b>New Klipper instance</b>
      <input type="hidden" name="kind" value="instance"><input name="name" placeholder="name" style="width:110px">
      <input name="port" placeholder="7130" style="width:70px"><input name="klipper_commit" placeholder="own Klipper commit (optional)" style="width:200px">
      <button class="ghost">Make script</button><pre style="width:100%;max-height:30vh;overflow:auto"></pre></form>
    <form onsubmit="makeScript(this);return false" class="row"><b>Retire an instance</b>
      <input type="hidden" name="kind" value="retire"><input name="name" placeholder="name (e.g. EnderCR10SE)" style="width:170px">
      <input name="port" placeholder="7127" style="width:70px">
      <button class="ghost">Make script</button><pre style="width:100%;max-height:30vh;overflow:auto"></pre></form>
    <div class="dim">Scripts are for a person to run on the host with sudo; Farm Control never runs root steps.</div></details>`;
}

function auditBlock(v) {
  if (v.audit_error) return `<div class="warn">audit: ${esc(v.audit_error)}</div>`;
  if (!v.audit) return `<div class="dim">audit needs Klipper to load its config (now ${esc(v.klippy_state)}: ${esc((v.state_message || "").slice(0, 120))})</div>`;
  const offNote = v.off ? `<div class="dim">printer switched off: changes still apply - Klipper loading the new config is the check; read the console after Reconnect</div>` : "";
  const fails = v.audit.filter((a) => a.status === "FAIL"), warns = v.audit.filter((a) => a.status === "WARN");
  const head = fails.length ? `<span class="bad">✗ ${fails.length} baseline gap${fails.length > 1 ? "s" : ""}</span>` : `<span class="ok">✓ farm baseline</span>`;
  const nfix = v.readonly ? 0 : v.audit.filter((a) => a.status !== "PASS" && a.fixable).length;   // WARN too: the macro library
  return `<details><summary>${head}${warns.length ? ` <span class="warn">· ${warns.length} warning</span>` : ""} — ${v.audit.length} rules</summary>${offNote}
    ${nfix > 1 ? `<div class="row" style="margin:6px 0"><button class="ghost" onclick='stageAllFixes(${js(v.key)}, ${nfix})'>Stage all ${nfix} fixes (one change per file)</button></div>` : ""}
    <table>${v.audit.map((a) => `<tr><td class="${a.status === "PASS" ? "ok" : a.status === "WARN" ? "warn" : "bad"}">${a.status}</td><td>${esc(a.rule)}</td><td class="dim">${esc(a.detail)}</td>
      <td>${a.status === "FAIL" && !v.readonly && !a.fixable ? `<span class="dim" title="needs judgement: use + Stage change">edit manually</span>` : ""}${a.status !== "PASS" && !v.readonly && a.fixable ? `<button class="ghost" onclick='stageFix(${js(v.key)}, ${js(a.rule)})'>Stage fix</button>` : ""}</td></tr>`).join("")}</table></details>`;
}

function configBlock(v) {
  const s = v.snapshot;
  if (!s || !s.latest) return `<div class="row">Config: <b>no snapshot yet</b> <button class="ghost" onclick="snap('${esc(v.key)}')">Snapshot now</button></div>`;
  const d = s.drift || {}, n = (d.changed || []).length + (d.added || []).length + (d.removed || []).length;
  const state = !s.reference ? `<span class="warn">not reviewed yet</span>`
    : n ? `<span class="${d.loud ? "bad" : "warn"}">drift: ${n} file${n > 1 ? "s" : ""} differ from reviewed</span>` : `<span class="ok">matches reviewed</span>`;
  return `<div class="row">Config: <b>${s.files} files</b> · snapshot ${s.latest.id} ${ago(s.latest.taken_at)} · ${state}
      ${s.reference ? `<span class="dim">reviewed #${s.reference.id} by ${esc(s.reference.reviewed_by)} ${ago(s.reference.reviewed_at)}</span>` : ""}</div>
    <div class="row"><button class="ghost" onclick="openFiles('${esc(v.key)}')">Files &amp; diff</button>
      ${v.registry && !v.readonly && Object.keys(data.host_services[v.host] || {}).some((n) => n.includes("telegram")) ? `<button class="ghost" onclick="telegramPack('${esc(v.key)}')">Telegram pack</button>` : ""}
      <button class="ghost" onclick="snap('${esc(v.key)}')">Snapshot now</button>
      ${s.reference && s.reference.id === s.latest.id ? "" : `<button class="ghost" onclick="review('${esc(v.key)}', ${s.latest.id})">Mark snapshot ${s.latest.id} reviewed</button>`}</div>`;
}

function changesBlock(v) {
  const cs = v.changes || [], pending = cs.filter((c) => c.state === "staged");
  const cls = { staged: "warn", applying: "warn", verifying: "warn", applied: "ok", failed: "bad", rolled_back: "dim", cancelled: "dim", rejected: "dim" };
  const row = (c) => `<tr><td>${c.id}</td><td class="${cls[c.state] || ""}">${esc(c.state)}</td><td>${esc(c.summary)}<div class="dim">${esc(c.reason)} · ${esc(c.author)}${c.applied_by ? " · applied by " + esc(c.applied_by) : ""}</div>
      ${c.error ? `<div class="bad">${esc(c.error)}</div>` : ""}</td><td style="white-space:nowrap">
      ${c.state === "staged" ? `<button class="ghost" onclick="previewChange(${c.id})">Diff</button> ${v.readonly ? "" : `<button onclick='changeAct(${c.id},"apply",${js(setOf(cs, c))})'>${setOf(cs, c).length > 1 ? `Apply set (${setOf(cs, c).length} files)` : "Apply"}</button>`} <button class="ghost" onclick="changeAct(${c.id},'cancel')">Cancel</button>` : ""}
      ${["applied", "failed"].includes(c.state) && !v.readonly ? `<button class="ghost" onclick='changeAct(${c.id},"rollback",${js(setOf(cs, c, true))})'>Rollback${setOf(cs, c, true).length > 1 ? " set" : ""}</button>` : ""}</td></tr>`;
  return `<details ${cs.length ? "open" : ""}><summary>Changes${pending.length ? ` · <span class="warn">${pending.length} staged</span>` : ""}${cs.length ? ` · ${cs.length} recent` : ""}</summary>
    ${cs.length ? `<table>${cs.map(row).join("")}</table>` : `<div class="dim">none yet</div>`}
    </details>
    ${v.readonly ? `<div class="dim">watch-only: no config changes</div>` : `<div class="row"><button class="ghost" onclick="openStage('${esc(v.key)}')">+ Stage change</button></div>`}`;
}

// changes staged as one set (a restore): applied and rolled back together, one restart
function setOf(cs, c, done) {
  if (!c.batch) return [c.id];
  return cs.filter((x) => x.batch === c.batch && (done ? ["applied", "failed"].includes(x.state) : x.state === "staged")).map((x) => x.id);
}

const when = (t) => t ? new Date(t * 1000).toLocaleString([], { year: "2-digit", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "?";
async function restorePoints(key, el) {
  const box = el.querySelector("div");
  const r = await (await fetch(`/api/fleet/${encodeURIComponent(key)}/restore-points`)).json();
  if (r.error) { box.textContent = r.error; return; }
  const k = js(key);
  const chip = (l) => `<span class="chip ${l === "last known good" ? "ok" : l === "origin" ? "warn" : ""}">${esc(l)}</span>`;
  const ev = (p) => p.evidence === "none" ? `<span class="dim">no completed print on record in this window</span>`
    : `<b>${p.prints.completed}</b> completed${p.prints.failed ? `, ${p.prints.failed} failed` : ""}${p.prints.good ? `, ${p.prints.good} marked good` : ""}${p.prints.bad ? `, <span class="bad">${p.prints.bad} marked bad</span>` : ""}
       <span class="${p.evidence === "exact" ? "ok" : "warn"}">(${p.evidence})</span>${p.prints.last ? `<div class="dim">last: ${esc(String(p.prints.last.file).split("/").pop())} · ${when(p.prints.last.finished_at)}</div>` : ""}`;
  const row = (p) => `<tr><td>${p.id}</td><td>${p.labels.map(chip).join(" ") || `<span class="dim">${esc(p.actor)}</span>`}
      <div class="dim">${p.kind === "origin" ? "before the farm's first change" : esc(p.actor)} · ${p.files} files</div></td>
    <td style="white-space:nowrap">${when(p.since)}<br>→ ${p.current ? "now" : when(p.until)}</td><td>${ev(p)}</td>
    <td style="white-space:nowrap">${p.current ? "" : `<button class="ghost" onclick='restoreDiff(${k}, ${p.id})'>Diff vs live</button> <button onclick='stageRestore(${k}, ${p.id}, ${js(p.labels.join(", ") || "snapshot " + p.id)})'>Stage restore</button>`}</td></tr>`;
  box.innerHTML = `<div class="dim">Each config was live from its newest file's time until the next change. A print that started and finished inside counts as evidence it works
      (<b>exact</b>: both ends known; <b>likely</b>: one end inferred). The original is pinned once and kept in git (printers/_archive). A restore is staged as one set and applied by a person: one restart, everything put back if Klipper doesn't load.</div>
    ${r.origin ? "" : `<div class="warn">No original pinned yet. <button class="ghost" onclick='pinOrigin(${k})'>Pin it now</button></div>`}
    <table>${r.points.map(row).join("")}</table>`;
}
async function restoreDiff(key, id) {
  $("fd-title").textContent = `${key}: restore point ${id}`; $("fd-sub").textContent = "live → restore point"; $("fd-files").innerHTML = "";
  $("fd-body").textContent = "loading…"; $("filesdlg").showModal();
  $("fd-body").innerHTML = colourDiff(await (await fetch(`/api/fleet/${encodeURIComponent(key)}/restore-diff?snapshot=${id}`)).text());
}
async function stageRestore(key, id, label) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  const reason = prompt(`Stage a restore of ${key} to ${label} (point ${id})? Every file that differs is staged as one set; nothing changes until you press Apply set on an idle printer.\n\nWhy?`, `restore ${label}`);
  if (reason === null) return;
  const r = await post(`/api/fleet/${encodeURIComponent(key)}/restore`, { snapshot: id, reason });
  if (r) { alert(`Staged changes ${r.changes.join(", ")} (${r.files.join(", ")}).${r.left_in_place.length ? "\nLeft in place: " + r.left_in_place.join(", ") : ""}\nReview each Diff, then Apply set.`); refresh(); }
}
async function pinOrigin(key) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  const r = await post(`/api/fleet/${encodeURIComponent(key)}/origin`);
  if (r) { alert(r.already ? `Already pinned (point ${r.id}).` : `Original pinned: point ${r.id}, ${r.files} files.`); refresh(); }
}

let cd = { key: null };
async function openStage(key) {
  cd = { key };
  $("cd-title").textContent = `Stage a change on ${key}`; $("cd-msg").textContent = ""; $("cd-preview").textContent = "";
  ["cd-section", "cd-option", "cd-value", "cd-reason"].forEach((i) => ($(i).value = ""));
  const r = await (await fetch(`/api/fleet/${encodeURIComponent(key)}/files`)).json();
  $("cd-path").innerHTML = (r.files || []).filter((f) => f.state !== "removed").map((f) => `<option ${f.path === "printer.cfg" ? "selected" : ""}>${esc(f.path)}</option>`).join("");
  document.querySelector('input[name=cdmode][value=key]').checked = true; cdMode();
  $("chgdlg").showModal();
}
function cdMode() {
  const file = document.querySelector("input[name=cdmode]:checked").value === "file";
  $("cd-key").style.display = file ? "none" : ""; $("cd-content").style.display = file ? "" : "none";
  if (file) loadFileForEdit();
}
async function loadFileForEdit() {
  if (document.querySelector("input[name=cdmode]:checked").value !== "file") return;
  $("cd-content").value = await (await fetch(`/api/fleet/${encodeURIComponent(cd.key)}/file?path=${encodeURIComponent($("cd-path").value)}`)).text();
}
async function submitChange() {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  const file = document.querySelector("input[name=cdmode]:checked").value === "file";
  const body = { path: $("cd-path").value, reason: $("cd-reason").value };
  if (file) body.content = $("cd-content").value; else Object.assign(body, { section: $("cd-section").value.trim(), option: $("cd-option").value.trim(), value: $("cd-value").value.trim() });
  const r = await post(`/api/fleet/${encodeURIComponent(cd.key)}/changes`, body);
  if (!r) return;
  $("cd-msg").textContent = `staged change ${r.id}: ${r.summary}. Review the diff, then Apply on the card when the printer is idle.`;
  const p = await post(`/api/changes/${r.id}/preview`);
  if (p) $("cd-preview").innerHTML = colourDiff(p.diff);
  refresh();
}
function colourDiff(t) {
  return t.split("\n").map((l) => `<span class="${l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : ""}">${esc(l)}</span>`).join("\n");
}
async function previewChange(id) {
  const p = await post(`/api/changes/${id}/preview`);
  if (!p) return;
  $("fd-title").textContent = `Change ${id}`; $("fd-sub").textContent = "staged vs live"; $("fd-files").innerHTML = "";
  $("fd-body").innerHTML = colourDiff(p.diff); $("filesdlg").showModal();
}
async function changeAct(id, action, set) {
  if (!operator()) { alert("Put your name in the 'You' box first (it's recorded with the change)."); return; }
  const many = (set || []).length > 1 ? ` together with ${set.filter((x) => x !== id).join(", ")} (one set, one restart)` : "";
  const words = { apply: "Apply change " + id + many + "? The printer must be idle: the files are uploaded and Klipper does a FIRMWARE_RESTART, then the change is verified (rolled back automatically if Klipper doesn't come back).",
                  rollback: "Roll back change " + id + many + "? The files go back to how they were, then FIRMWARE_RESTART.", cancel: "Cancel staged change " + id + "?" };
  if (!confirm(words[action])) return;
  document.body.style.cursor = "progress";
  const r = await post(`/api/changes/${id}/${action}`);
  document.body.style.cursor = "";
  if (r) alert(`Change ${id}: ${r.state}${r.error ? " - " + r.error : ""}`);
  refresh();
}

function maintBlock(v) {
  if (!v.maintenance) return "";
  const due = v.maintenance.filter((m) => m.due), soon = v.maintenance.filter((m) => m.soon);
  const row = (m) => `<tr><td>${esc(m.kind.replace("_", " "))}</td>
    <td class="${m.due ? "bad" : m.soon ? "warn" : m.no_record ? "dim" : "ok"}">${m.no_record ? "no record" : `${m.since_hours ?? "?"} h since`}</td>
    <td class="dim">${m.interval ? "every " + m.interval + " h" : "no reminder"}</td>
    <td><button class="ghost" onclick='maintDone(${js(v.key)}, ${js(m.kind)})'>Done</button>
        <button class="ghost" title="reminder interval in print hours" onclick='maintInterval(${js(v.key)}, ${js(m.kind)}, ${m.interval || 0})'>⏱</button></td></tr>`;
  return `<details ${due.length ? "open" : ""}><summary>Maintenance${due.length ? ` · <span class="bad">${due.length} due</span>` : ""}${soon.length ? ` · <span class="warn">${soon.length} soon</span>` : ""} · ${v.print_hours ?? "?"} print h</summary>
    <table>${v.maintenance.map(row).join("")}</table></details>`;
}
async function maintDone(key, kind) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  const note = prompt(`${kind.replace("_", " ")} on ${key} done - note (optional):`, "");
  if (note === null) return;
  if (await post(`/api/fleet/${encodeURIComponent(key)}/maintenance`, { kind, note })) refresh();
}
async function maintInterval(key, kind, cur) {
  const h = prompt(`Remind about ${kind.replace("_", " ")} every how many print hours? (0 = no reminder; applies to every printer)`, cur);
  if (h === null) return;
  if (await post(`/api/fleet/${encodeURIComponent(key)}/maintenance`, { kind, interval: +h })) refresh();
}

function onboardBlock(v) {
  if (v.registry) return farmSettings(v);
  const k = js(v.key);
  const fails = (v.audit || []).filter((a) => a.status === "FAIL").length;
  const steps = [
    ["reachable", v.reachable, ""],
    ["board connected (Klipper ready)", v.klippy_state === "ready",
      `<div class="dim">${esc((v.state_message || "").split("\n")[0].slice(0, 160))}</div>` +
      (/Unable to open config file/.test(v.state_message || "")
        ? `<button class="ghost" onclick='stageMissingInclude(${k})'>Stage: comment out the missing include</button>`
        : v.off ? `<button class="ghost" onclick='reconnectPrinter(${k})'>Reconnect (after switching it on)</button> `
                + `<button class="ghost" onclick='findSerial(${k}, ${js(v.host)}, this.parentNode)'>Find the board's USB port</button>`
        : `<button class="ghost" onclick='findSerial(${k}, ${js(v.host)}, this.parentNode)'>Find the board's USB port</button>`)],
    ["farm baseline audit passes", v.audit && !fails, fails ? `<span class="dim">Stage fix on the ${fails} failing rule(s) above, then Apply</span>` : ""],
    ["camera registered and working", (v.cameras || []).some((c) => c.ok),
      `<input placeholder="webcam3" style="width:90px"> <button class="ghost" onclick='registerCam(${k}, this.previousElementSibling.value)'>Register</button>
       <span class="dim">the host's web path (USB devices &amp; host scripts → Camera service makes one)</span>`],
    ["config reviewed", v.snapshot && v.snapshot.reference, `<span class="dim">Config → Snapshot, look, Mark reviewed</span>`]];
  const left = steps.filter((s) => !s[1]).length + 1;
  const profs = data.profiles.map((p) => `<label title="${esc(p.name)}"><input type="checkbox" name="profiles" value="${esc(p.id)}"> ${esc(p.id)}${p.confirmed ? "" : " (unconfirmed)"}</label>`).join(" ");
  return `<details class="onboard"><summary class="warn">Not onboarded — ${left} step(s) left</summary>
    <table>${steps.map(([t, ok, act]) => `<tr><td class="${ok ? "ok" : "bad"}">${ok ? "✓" : "✗"}</td><td>${esc(t)}</td><td>${ok ? "" : act}</td></tr>`).join("")}
      <tr><td class="bad">✗</td><td>in the farm registry</td><td></td></tr></table>
    <form onsubmit='onboard(${k}, this, ${left - 1});return false' style="display:grid;gap:6px;margin-top:8px">
      <div class="row">Name <input name="name" value="${esc(v.name)}" style="width:130px" required>
        Note <input name="note" style="flex:1;min-width:160px" placeholder="hardware, nozzle, quirks"></div>
      <div class="row">Usable bed X <input name="x0" value="0" style="width:52px">–<input name="x1" placeholder="220" style="width:52px" required>
        Y <input name="y0" value="0" style="width:52px">–<input name="y1" placeholder="220" style="width:52px" required>
        max Z <input name="z" placeholder="250" style="width:56px" required> mm</div>
      <div class="row">Profiles ${profs}</div>
      <div class="row"><label><input type="checkbox" name="m600"> M600 shows a prompt</label>
        <label><input type="checkbox" name="readonly"> watch-only (no jobs, no changes)</label>
        <button>Add to the farm</button></div>
      <div class="dim">Bed and Z are what you measured, not what a copied config says. Each profile's bed must fit, or nothing is saved.</div>
    </form></details>`;
}
function farmSettings(v) {
  const e = (data.registry || []).find((x) => x.name === v.registry);
  if (!e) return "";
  const k = js(v.registry);
  const profs = data.profiles.map((p) => `<label><input type="checkbox" name="profiles" value="${esc(p.id)}" ${e.profiles.includes(p.id) ? "checked" : ""}> ${esc(p.id)}</label>`).join(" ");
  return `<details><summary>Farm settings · ${e.profiles.length ? esc(e.profiles.join(", ")) : "no profiles"}</summary>
    <form onsubmit='saveSettings(${k}, this);return false' style="display:grid;gap:6px;margin-top:6px">
      <div class="row">Bed ${esc(e.bed_min.join(","))} – ${esc(e.bed_max.join(","))} · Z ${e.max_z}${e.note ? " · " + esc(e.note) : ""}</div>
      <div class="row">Profiles ${profs}</div>
      <div class="row"><label><input type="checkbox" name="readonly" ${e.readonly ? "checked" : ""}> watch-only</label><button class="ghost">Save</button></div>
    </form></details>`;
}
async function fluiddPreview(key, el) {
  const box = el.querySelector("div");
  const r = await (await fetch(`/api/fleet/${encodeURIComponent(key)}/fluidd`)).json();
  if (r.error) { box.textContent = r.error; return; }
  const k = js(key);
  box.innerHTML = `<div class="dim">Macro panel grouped by each macro's description; farm temperature presets first. Only the macro panel and presets change; Undo puts back what was there.</div>
    <table>${Object.entries(r.groups).map(([g, ms]) => `<tr><td>${esc(g)}</td><td class="dim">${ms.map(esc).join(", ")}</td></tr>`).join("")}
      <tr><td>Presets</td><td class="dim">${r.presets.map(esc).join(", ")}</td></tr></table>
    <button onclick='fluiddApply(${k})'>Apply farm layout</button>
    ${r.last && !r.last.undone_at ? `<button class="ghost" onclick='fluiddUndo(${k}, ${r.last.id})'>Undo last (${esc(r.last.actor)})</button>` : ""}`;
}
async function fluiddApply(key) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  if (!confirm(`Write the farm Fluidd layout into ${key}'s Fluidd settings? (no printer config, no restart)`)) return;
  const r = await post(`/api/fleet/${encodeURIComponent(key)}/fluidd`);
  if (r) { alert(`Done (push ${r.id}). Reload Fluidd to see it.`); refresh(); }
}
async function fluiddUndo(key, id) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  const r = await post(`/api/fleet/${encodeURIComponent(key)}/fluidd-undo`, { id });
  if (r) { alert(`Restored: ${r.restored.join(", ")}`); refresh(); }
}
async function reconnectPrinter(key) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  if (!confirm(`Reconnect ${key}? Switch it on first; Klipper restarts to find the board.`)) return;
  document.body.style.cursor = "wait";
  const r = await post(`/api/fleet/${encodeURIComponent(key)}/reconnect`);
  document.body.style.cursor = "";
  if (r) alert(`${key}: ${r.state}${r.hint ? " - " + r.hint : ""}`);
  refresh();
}
async function stageMissingInclude(key) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  const r = await post(`/api/fleet/${encodeURIComponent(key)}/missing-include`);
  if (r) { alert(r.map((c) => `Staged change ${c.id}: ${c.summary}`).join("\n") + "\n\nLook at the Diff, then Apply it under Changes."); refresh(); }
}
async function findSerial(key, host, td) {
  const r = await (await fetch(`/api/hosts/${encodeURIComponent(host)}/usb`)).json();
  if (r.error) { td.innerHTML = `<span class="warn">${esc(r.error)}</span>`; return; }
  const free = r.serial.filter((d) => !d.used_by);
  td.innerHTML = free.length ? free.map((d) => `<div><code>${esc(d.path.replace("/dev/serial/by-path/", ""))}</code> <span class="dim">${esc(d.target)}</span>
      <button class="ghost" onclick='setSerial(${js(key)}, ${js(d.path)})'>Use this</button></div>`).join("")
    : `<span class="warn">no unused serial device on ${esc(host)} — is the board plugged in?</span>`;
}
async function setSerial(key, port) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  const r = await post(`/api/fleet/${encodeURIComponent(key)}/serial`, { port });
  if (r) { alert(`Staged change ${r.id}: ${r.summary}. Apply it under Changes (Klipper restarts and should then find the board).`); refresh(); }
}
async function registerCam(key, path) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  if (!path.trim()) { alert("The web path, e.g. webcam3"); return; }
  const r = await post(`/api/fleet/${encodeURIComponent(key)}/camera`, { path: path.trim() });
  if (r) { alert(`Registered ${r.registered} (${r.stream}).`); refresh(); }
}
async function onboard(key, form, left) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  const f = new FormData(form), n = (x) => +f.get(x);
  const body = { name: f.get("name").trim(), note: f.get("note"), bed_min: [n("x0"), n("y0")], bed_max: [n("x1"), n("y1")], max_z: n("z"),
    profiles: f.getAll("profiles"), has_m600_prompt: !!f.get("m600"), readonly: !!f.get("readonly") };
  if (!confirm(`Add ${body.name} to the farm: bed ${body.bed_min}–${body.bed_max}, Z ${body.max_z}, profiles ${body.profiles.join(", ") || "none"}${body.readonly ? ", watch-only" : ""}?` +
    (left ? `\n\n${left} checklist step(s) are still open.` : "") + `\n\nThe monitor and the pipeline pick it up at once; FARM.md's change log records it.`)) return;
  const r = await post(`/api/fleet/${encodeURIComponent(key)}/onboard`, body);
  if (r) { alert(`${r.name} is in the farm. Commit printers/registry.json and FARM.md in the next development session.`); refresh(); }
}
async function saveSettings(name, form) {
  if (!operator()) { alert("Put your name in the 'You' box first."); return; }
  const f = new FormData(form), body = { profiles: f.getAll("profiles"), readonly: !!f.get("readonly") };
  if (!confirm(`Save ${name}: profiles ${body.profiles.join(", ") || "none"}${body.readonly ? ", watch-only" : ""}?`)) return;
  const r = await post(`/api/fleet/${encodeURIComponent(name)}/registry`, body);
  if (r) { alert(r.changed.length ? `Saved: ${r.changed.join("; ")}` : "Nothing changed."); refresh(); }
}

// A printer card follows the layout kept in this browser (static/ui.js): order, collapsed, hidden. One with something
// waiting on a person (unreachable, Klipper not ready, staged changes, maintenance due) is never hidden, only collapsed,
// and its collapsed line says what is waiting.
function frame(v, i, head, body, waiting, urgent) {
  const st = farmUI.cardState("fleet", v.key, i);
  const collapsed = st.collapsed || (st.hidden && urgent), hide = st.hidden && !urgent;
  return `<div class="card ccard fcard${collapsed ? " collapsed" : ""}${hide ? " is-hidden" : ""}" data-card="${esc(v.key)}" data-scope="fleet" style="order:${st.order}">
    <div class="ph"><div class="phl">${head}</div><span class="end">${farmUI.cardControls("fleet", v.key)}</span></div>
    ${collapsed ? (waiting.length ? `<div class="row">${waiting.join(" ")}</div>` : "") : body}</div>`;
}

function card(v, i) {
  if (!v.reachable) return frame(v, i, `<span class="name">${esc(v.host)}</span><span class="dim">:${v.port}</span><span class="chip bad">unreachable</span>`,
    `<div class="dim">${esc(v.error || "")}</div>`, [], true);
  const pending = (v.changes || []).filter((c) => c.state === "staged").length, due = (v.maintenance || []).filter((m) => m.due).length;
  const fails = (v.audit || []).filter((a) => a.status === "FAIL").length;
  const waiting = [pending ? `<span class="chip warn">${pending} staged</span>` : "", due ? `<span class="chip bad">${due} maintenance due</span>` : "",
    v.klippy_state !== "ready" && !v.off ? `<span class="chip bad">Klipper ${esc(v.klippy_state || "?")}</span>` : ""].filter(Boolean);
  const urgent = waiting.length > 0;
  const badge = v.readonly ? `<span class="chip warn">watch-only</span>` : v.registry ? `<span class="chip ok">onboarded</span>` : `<span class="chip warn">not onboarded</span>`;
  const st = v.off ? `<span class="chip">switched off</span>${v.readonly ? "" : ` <button class="ghost" onclick='reconnectPrinter(${js(v.key)})'>Reconnect</button>`}`
    : v.klippy_state !== "ready" ? `<span class="chip bad">${esc(v.klippy_state || "?")}</span>` : `<span class="chip ${v.paused ? "warn" : v.job === "printing" ? "ok" : ""}">${esc(v.paused ? "paused" : v.job || "?")}</span>`;
  const vers = Object.entries(v.versions || {}).map(([k, ver]) => `<span class="chip ${(v.updates || []).includes(k) ? "warn" : ""}" title="${(v.updates || []).includes(k) ? "update available" : ""}">${esc(k.replace("Klipper-Adaptive-Meshing-Purging", "KAMP").replace("moonraker-telegram-bot", "telegram-bot"))} ${esc(ver)}</span>`).join(" ");
  const cams = (v.cameras || []).map((c) => `<span class="chip ${c.ok ? "ok" : "bad"}">📷 ${esc(c.name)} ${c.ok ? "ok" : "no snapshot"}</span>`).join(" ") || `<span class="dim">no camera registered</span>`;
  if (fails) waiting.push(`<span class="chip warn">${fails} baseline gap${fails > 1 ? "s" : ""}</span>`);   // shown when collapsed, never forces a hidden card back
  const head = `<span class="name">${esc(v.registry || v.name)}</span><span class="dim">${esc(v.host)}:${v.port}</span>${badge}${st}
      <a class="lnk" href="${esc(v.fluidd)}" target="_blank" rel="noopener">Fluidd ↗</a>`;
  return frame(v, i, head, `
    ${v.klippy_state !== "ready" && v.state_message && !v.off ? `<div class="warn" style="font-size:12px">${esc(v.state_message)}</div>` : ""}
    <div class="row">Print hours <b>${v.print_hours ?? "–"}</b> Jobs <b>${v.total_jobs ?? "–"}</b> ${v.file ? `File <b>${esc(v.file.split("/").pop())}</b>` : ""}</div>
    <div class="row">${cams}</div>
    ${(v.failed_components || []).length ? `<div class="bad">failed components: ${esc(v.failed_components.join(", "))}</div>` : ""}
    ${(v.warnings || []).length ? `<div class="warn" style="font-size:12px">${v.warnings.map(esc).join("<br>")}</div>` : ""}
    ${auditBlock(v)}
    ${configBlock(v)}
    ${changesBlock(v)}
    ${maintBlock(v)}
    ${v.reachable ? `<details ontoggle='if (this.open) restorePoints(${js(v.key)}, this)'><summary>Restore points (original · last known good)</summary><div class="dim">loading…</div></details>` : ""}
    ${onboardBlock(v)}
    ${v.reachable && v.klippy_state === "ready" && !v.readonly ? `<details ontoggle='if (this.open) fluiddPreview(${js(v.key)}, this)'><summary>Fluidd layout (farm standard)</summary><div class="dim">loading…</div></details>` : ""}
    <details><summary>versions${(v.updates || []).length ? ` · <span class="warn">${v.updates.length} update(s) available</span>` : ""}</summary><div class="cluster">${vers}</div></details>`,
    waiting, urgent);
}

function render(force) {
  const inst = data.instances, up = inst.filter((v) => v.reachable);
  const fails = up.reduce((a, v) => a + (v.audit ? v.audit.filter((x) => x.status === "FAIL").length : 0), 0);
  const drift = up.filter((v) => v.snapshot && v.snapshot.drift && v.snapshot.reference && ((v.snapshot.drift.changed || []).length + (v.snapshot.drift.added || []).length + (v.snapshot.drift.removed || []).length)).length;
  const badSvc = Object.values(data.host_services).reduce((a, s) => a + Object.entries(s).filter(([n, x]) => /auto-restart|failed/.test(x)).length, 0);
  $("kpis").innerHTML = [[`${up.length}/${inst.length}`, "instances up"], [up.filter((v) => v.registry).length, "onboarded"],
    [fails, "baseline gaps"], [drift, "with drift"], [badSvc, "services failing"], [new Date(data.time * 1000).toLocaleTimeString(), "updated"]]
    .map(([v, l]) => `<div class="kpi"><b>${esc(v)}</b><small>${esc(l)}</small></div>`).join("");
  const box = $("hosts");
  // not while someone is working in it: a drag, an open menu, a field with focus, or a block that loads on opening
  // (restore points, Fluidd layout) - the next refresh after that redraws
  if (farmUI.busy(box) || (!force && box.querySelector("details[open][ontoggle]"))) return;
  box.innerHTML = data.hosts.map((h) => {
    const mine = inst.filter((v) => v.host === h.name), any = mine.find((v) => v.reachable);
    const stats = any ? `CPU <b>${any.cpu_temp != null ? Math.round(any.cpu_temp) + "°C" : "–"}</b> RAM <b>${any.mem_used_pct ?? "–"}%</b> Disk <b class="${any.disk_used_pct > 90 ? "bad" : ""}">${any.disk_used_pct ?? "–"}%</b> (${any.disk_free_gb ?? "–"} GB free)${(any.throttled || []).length ? ` <span class="bad">throttled: ${esc(any.throttled.join(","))}</span>` : ""}` : "";
    return `<section class="panel host" data-panel="${esc(h.name)}">
      <div class="panel-h"><h2>${esc(h.name)} <span class="dim">${esc(h.address)}${h.note ? " · " + esc(h.note) : ""}</span></h2>
        ${any ? `<span class="row hoststats">${stats}</span>` : ""}</div>
      <div class="panel-b stack">
        ${any ? `<div class="row">${serviceChips(data.host_services[h.name], h.name)}</div>${hostTools(h)}` : ""}
        <div class="grid" data-cards="fleet">${mine.map(card).join("")}</div></div></section>`;
  }).join("");
  box.querySelectorAll("details:not(.menu)").forEach((d) => { if (d.style.display !== "inline-block") d.classList.add("sub"); });
  restoreDetails(box);
  farmUI.panels();
}

// a refresh keeps the <details> a person opened or closed (by card and position); the others keep their own default,
// e.g. Changes opens by itself once something is staged
const userOpen = new Map();
function detailsKeys(box) {
  return [...box.querySelectorAll("details:not(.menu)")].map((d) => {
    const owner = d.closest("[data-card]") || d.closest("[data-panel]");
    const peers = owner ? [...owner.querySelectorAll("details:not(.menu)")] : [];
    return [d, `${owner ? owner.dataset.card || owner.dataset.panel : ""}#${peers.indexOf(d)}`];
  });
}
function restoreDetails(box) { detailsKeys(box).forEach(([d, k]) => { if (userOpen.has(k) && !d.hasAttribute("ontoggle")) d.open = userOpen.get(k); }); }
document.addEventListener("click", (e) => {
  const sum = e.target.closest && e.target.closest("#hosts details:not(.menu) > summary");
  if (!sum) return;
  setTimeout(() => { const hit = detailsKeys($("hosts")).find(([d]) => d === sum.parentElement); if (hit) userOpen.set(hit[1], hit[0].open); }, 0);
});

async function refresh() {
  try { data = await (await fetch("/api/fleet")).json(); render(); }
  catch (e) { $("kpis").innerHTML = `<div class="kpi"><b class="bad">offline</b><small>farm server</small></div>`; }
}

async function snap(key) { const r = await post(`/api/fleet/${encodeURIComponent(key)}/snapshot`); if (r) { alert(r.unchanged ? "No change since the last snapshot." : `Snapshot ${r.id}: ${r.files} files.`); refresh(); } }
async function review(key, id) {
  if (!operator()) { alert("Put your name in the 'You' box first (it's recorded as the reviewer)."); return; }
  if (!confirm(`Mark snapshot ${id} of ${key} as the reviewed, known-good config? Drift is measured against it from now on.`)) return;
  if (await post(`/api/fleet/${encodeURIComponent(key)}/review`, { snapshot: id })) refresh();
}

async function openFiles(key) {
  fd = { key, files: [], path: null, mode: "diff" };
  $("fd-title").textContent = key; $("fd-body").textContent = "loading…"; $("filesdlg").showModal();
  const r = await (await fetch(`/api/fleet/${encodeURIComponent(key)}/files`)).json();
  fd.files = r.files || [];
  $("fd-sub").textContent = `snapshot ${r.latest ?? "–"} · reviewed ${r.reference ?? "none"}`;
  const cls = { changed: "warn", added: "warn", removed: "bad", same: "ok", unreviewed: "dim" };
  $("fd-files").innerHTML = fd.files.map((f, i) => `<button id="fdf-${i}" onclick="showFile(${i})"><span>${esc(f.path)}</span><span class="${cls[f.state]}">${esc(f.state)}</span></button>`).join("");
  const first = fd.files.findIndex((f) => f.state !== "same");
  showFile(first >= 0 ? first : 0);
}
function toggleMode() { fd.mode = fd.mode === "diff" ? "file" : "diff"; $("fd-mode").textContent = fd.mode === "diff" ? "show file" : "show diff"; if (fd.path != null) showFile(fd.path); }
async function showFile(i) {
  fd.path = i;
  document.querySelectorAll(".files button").forEach((b) => b.classList.remove("sel"));
  const btn = $(`fdf-${i}`); if (btn) btn.classList.add("sel");
  const f = fd.files[i]; if (!f) { $("fd-body").textContent = "no files"; return; }
  const url = `/api/fleet/${encodeURIComponent(fd.key)}/${fd.mode === "diff" ? "diff" : "file"}?path=${encodeURIComponent(f.path)}`;
  const t = await (await fetch(url)).text();
  $("fd-body").innerHTML = fd.mode === "diff"
    ? t.split("\n").map((l) => `<span class="${l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : ""}">${esc(l)}</span>`).join("\n")
    : esc(t);
}

refresh(); setInterval(refresh, 20000);
farmUI.onCards("fleet", () => { if (data) render(true); });       // a layout change redraws at once
