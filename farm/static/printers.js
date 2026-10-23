"use strict";
// Printers tab: fleet health, farm-baseline audit, config snapshots and drift. Read-only towards the printers;
// the only actions are "snapshot now" and "mark reviewed" (farm DB). Staged changes come in phase 2.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const ls = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } };
const ago = (t) => { if (!t) return "never"; const s = Date.now() / 1000 - t; return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`; };
let data = null, fd = { key: null, files: [], path: null, mode: "diff" };

function setOperator(v) { ls("operator", v.trim()); }
function operator() { return ls("operator") || ""; }

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
      ${any ? `<a href="#" class="lnk" title="restart ${esc(n)}" onclick='restartSvc(${JSON.stringify(any.key)}, ${JSON.stringify(n)});return false'>↻</a>` : ""}</span>`;
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
    <div class="dim">Scripts are for a person to run on the host with sudo; Farm Control never runs root steps.</div></details>`;
}

function auditBlock(v) {
  if (v.audit_error) return `<div class="warn">audit: ${esc(v.audit_error)}</div>`;
  if (!v.audit) return `<div class="dim">audit needs Klipper ready (now ${esc(v.klippy_state)})</div>`;
  const fails = v.audit.filter((a) => a.status === "FAIL"), warns = v.audit.filter((a) => a.status === "WARN");
  const head = fails.length ? `<span class="bad">✗ ${fails.length} baseline gap${fails.length > 1 ? "s" : ""}</span>` : `<span class="ok">✓ farm baseline</span>`;
  return `<details><summary>${head}${warns.length ? ` <span class="warn">· ${warns.length} warning</span>` : ""} — ${v.audit.length} rules</summary>
    <table>${v.audit.map((a) => `<tr><td class="${a.status === "PASS" ? "ok" : a.status === "WARN" ? "warn" : "bad"}">${a.status}</td><td>${esc(a.rule)}</td><td class="dim">${esc(a.detail)}</td>
      <td>${a.status === "FAIL" && !v.readonly && !a.fixable ? `<span class="dim" title="needs judgement: use + Stage change">edit manually</span>` : ""}${a.status === "FAIL" && !v.readonly && a.fixable ? `<button class="ghost" onclick='stageFix(${JSON.stringify(v.key)}, ${JSON.stringify(a.rule)})'>Stage fix</button>` : ""}</td></tr>`).join("")}</table></details>`;
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
      ${c.state === "staged" ? `<button class="ghost" onclick="previewChange(${c.id})">Diff</button> ${v.readonly ? "" : `<button onclick="changeAct(${c.id},'apply')">Apply</button>`} <button class="ghost" onclick="changeAct(${c.id},'cancel')">Cancel</button>` : ""}
      ${["applied", "failed"].includes(c.state) && !v.readonly ? `<button class="ghost" onclick="changeAct(${c.id},'rollback')">Rollback</button>` : ""}</td></tr>`;
  return `<details ${cs.length ? "open" : ""}><summary>Changes${pending.length ? ` · <span class="warn">${pending.length} staged</span>` : ""}${cs.length ? ` · ${cs.length} recent` : ""}</summary>
    ${cs.length ? `<table>${cs.map(row).join("")}</table>` : `<div class="dim">none yet</div>`}
    </details>
    ${v.readonly ? `<div class="dim">watch-only: no config changes</div>` : `<div class="row"><button class="ghost" onclick="openStage('${esc(v.key)}')">+ Stage change</button></div>`}`;
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
async function changeAct(id, action) {
  if (!operator()) { alert("Put your name in the 'You' box first (it's recorded with the change)."); return; }
  const words = { apply: "Apply change " + id + "? The printer must be idle: the file is uploaded and Klipper does a FIRMWARE_RESTART, then the change is verified (rolled back automatically if Klipper doesn't come back).",
                  rollback: "Roll back change " + id + "? The file goes back to how it was, then FIRMWARE_RESTART.", cancel: "Cancel staged change " + id + "?" };
  if (!confirm(words[action])) return;
  document.body.style.cursor = "progress";
  const r = await post(`/api/changes/${id}/${action}`);
  document.body.style.cursor = "";
  if (r) alert(`Change ${id}: ${r.state}${r.error ? " - " + r.error : ""}`);
  refresh();
}

function onboardBlock(v) {
  if (v.registry) return "";
  const items = [
    ["reachable", v.reachable], ["Klipper ready (board connected)", v.klippy_state === "ready"],
    ["farm baseline audit passes", v.audit && !v.audit.some((a) => a.status === "FAIL")],
    ["camera works", (v.cameras || []).some((c) => c.ok)], ["config reviewed", v.snapshot && v.snapshot.reference],
    ["in the farm registry (farm/printers.py)", false]];
  return `<details><summary class="warn">Not onboarded — ${items.filter((i) => !i[1]).length} step(s) left</summary>
    <table>${items.map(([t, ok]) => `<tr><td class="${ok ? "ok" : "bad"}">${ok ? "✓" : "✗"}</td><td>${esc(t)}</td></tr>`).join("")}</table></details>`;
}

function card(v) {
  if (!v.reachable) return `<div class="card"><div class="ph"><span class="name">${esc(v.host)}</span><span class="dim">:${v.port}</span><span class="chip bad">unreachable</span></div><div class="dim">${esc(v.error || "")}</div></div>`;
  const badge = v.readonly ? `<span class="chip warn">watch-only</span>` : v.registry ? `<span class="chip ok">onboarded</span>` : `<span class="chip warn">not onboarded</span>`;
  const st = v.klippy_state !== "ready" ? `<span class="chip bad">${esc(v.klippy_state || "?")}</span>` : `<span class="chip ${v.paused ? "warn" : v.job === "printing" ? "ok" : ""}">${esc(v.paused ? "paused" : v.job || "?")}</span>`;
  const vers = Object.entries(v.versions || {}).map(([k, ver]) => `<span class="chip ${(v.updates || []).includes(k) ? "warn" : ""}" title="${(v.updates || []).includes(k) ? "update available" : ""}">${esc(k.replace("Klipper-Adaptive-Meshing-Purging", "KAMP").replace("moonraker-telegram-bot", "telegram-bot"))} ${esc(ver)}</span>`).join(" ");
  const cams = (v.cameras || []).map((c) => `<span class="chip ${c.ok ? "ok" : "bad"}">📷 ${esc(c.name)} ${c.ok ? "ok" : "no snapshot"}</span>`).join(" ") || `<span class="dim">no camera registered</span>`;
  return `<div class="card">
    <div class="ph"><span class="name">${esc(v.registry || v.name)}</span><span class="dim">${esc(v.host)}:${v.port}</span>${badge}${st}
      <a class="lnk" href="${esc(v.fluidd)}" target="_blank" rel="noopener">Fluidd ↗</a></div>
    ${v.klippy_state !== "ready" && v.state_message ? `<div class="warn" style="font-size:12px">${esc(v.state_message)}</div>` : ""}
    <div class="row">Print hours <b>${v.print_hours ?? "–"}</b> Jobs <b>${v.total_jobs ?? "–"}</b> ${v.file ? `File <b>${esc(v.file.split("/").pop())}</b>` : ""}</div>
    <div class="row">${cams}</div>
    ${(v.failed_components || []).length ? `<div class="bad">failed components: ${esc(v.failed_components.join(", "))}</div>` : ""}
    ${(v.warnings || []).length ? `<div class="warn" style="font-size:12px">${v.warnings.map(esc).join("<br>")}</div>` : ""}
    ${auditBlock(v)}
    ${configBlock(v)}
    ${changesBlock(v)}
    ${onboardBlock(v)}
    <details><summary>versions${(v.updates || []).length ? ` · <span class="warn">${v.updates.length} update(s) available</span>` : ""}</summary><div style="margin-top:6px">${vers}</div></details>
  </div>`;
}

function render() {
  const inst = data.instances, up = inst.filter((v) => v.reachable);
  const fails = up.reduce((a, v) => a + (v.audit ? v.audit.filter((x) => x.status === "FAIL").length : 0), 0);
  const drift = up.filter((v) => v.snapshot && v.snapshot.drift && v.snapshot.reference && ((v.snapshot.drift.changed || []).length + (v.snapshot.drift.added || []).length + (v.snapshot.drift.removed || []).length)).length;
  const badSvc = Object.values(data.host_services).reduce((a, s) => a + Object.entries(s).filter(([n, x]) => /auto-restart|failed/.test(x)).length, 0);
  $("kpis").innerHTML = [[`${up.length}/${inst.length}`, "instances up"], [up.filter((v) => v.registry).length, "onboarded"],
    [fails, "baseline gaps"], [drift, "with drift"], [badSvc, "services failing"], [new Date(data.time * 1000).toLocaleTimeString(), "updated"]]
    .map(([v, l]) => `<div class="kpi"><b>${esc(v)}</b><small>${esc(l)}</small></div>`).join("");
  $("hosts").innerHTML = data.hosts.map((h) => {
    const mine = inst.filter((v) => v.host === h.name), any = mine.find((v) => v.reachable);
    const stats = any ? `CPU <b>${any.cpu_temp != null ? Math.round(any.cpu_temp) + "°C" : "–"}</b> RAM <b>${any.mem_used_pct ?? "–"}%</b> Disk <b class="${any.disk_used_pct > 90 ? "bad" : ""}">${any.disk_used_pct ?? "–"}%</b> (${any.disk_free_gb ?? "–"} GB free)${(any.throttled || []).length ? ` <span class="bad">throttled: ${esc(any.throttled.join(","))}</span>` : ""}` : "";
    return `<section class="host"><h2>${esc(h.name)} <span class="dim" style="text-transform:none;letter-spacing:0">${esc(h.address)}${h.note ? " · " + esc(h.note) : ""}</span>
        ${any ? `<span class="row" style="text-transform:none;letter-spacing:0">${stats}</span>` : ""}</h2>
      ${any ? `<div class="row" style="margin-bottom:8px">${serviceChips(data.host_services[h.name], h.name)}</div><div style="margin-bottom:8px">${hostTools(h)}</div>` : ""}
      <div class="grid">${mine.map(card).join("")}</div></section>`;
  }).join("");
}

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

$("operator").value = operator();
refresh(); setInterval(refresh, 20000);
