// Farm Control display settings and editable layout, per browser (like Fluidd's layout editing, minus the server).
// Loaded in <head> so the theme is on <html> before the first paint. Nothing here talks to the server: the layout is
// a display preference kept in localStorage ("farm.ui.v1"), so a phone and a PC can differ and a broken store only
// means the default layout.
//
//   settings   theme system|light|dark, density comfortable|compact, text s|m|l, cards s|m|l -> attributes on <html>
//   panels     <section class="panel" data-panel="id"> with a .panel-h header and a .panel-b body, inside a container
//              marked data-panels="scope": collapse, and in edit mode drag / up / down / hide. Order is CSS `order`,
//              so nothing is moved in the DOM.
//   cards      cards a page re-renders on every poll ([data-card="id"] in a data-cards="scope" container). The page
//              asks cardState() while it builds the HTML, so a re-render keeps the layout; it adds cardControls() to
//              each card header and calls onCards() to be re-rendered after a change.
"use strict";
(function () {
  const KEY = "farm.ui.v1";
  const root = document.documentElement;
  const page = () => root.dataset.page || "page";
  let S = {};
  try { S = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { S = {}; }
  if (typeof S !== "object" || Array.isArray(S)) S = {};
  S.L = S.L && typeof S.L === "object" ? S.L : {};
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) { /* private window: this visit only */ } };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const CHOICES = {
    theme: [["system", "System"], ["light", "Light"], ["dark", "Dark"]],
    density: [["comfortable", "Comfortable"], ["compact", "Compact"]],
    text: [["s", "S"], ["m", "M"], ["l", "L"]],
    cards: [["s", "S"], ["m", "M"], ["l", "L"]],
  };
  const DEFAULTS = { theme: "system", density: "comfortable", text: "m", cards: "m" };
  const get = (k) => (CHOICES[k].some(([v]) => v === S[k]) ? S[k] : DEFAULTS[k]);

  function applySettings() {
    const t = get("theme");
    if (t === "system") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", t);
    root.setAttribute("data-density", get("density"));
    root.setAttribute("data-text", get("text"));
    root.setAttribute("data-cards", get("cards"));
  }
  applySettings();

  // ------------------------------------------------------------ layout state per page + scope
  const lay = (scope) => {
    const k = `${page()}/${scope}`;
    const l = S.L[k] && typeof S.L[k] === "object" ? S.L[k] : (S.L[k] = {});
    for (const f of ["order", "hidden", "collapsed"]) if (!Array.isArray(l[f])) l[f] = [];
    if (!l.parts || typeof l.parts !== "object") l.parts = {};
    return l;
  };
  const has = (arr, id) => arr.includes(String(id));
  const toggleIn = (arr, id, on) => {
    id = String(id);
    const i = arr.indexOf(id);
    if (on === undefined) on = i < 0;
    if (on && i < 0) arr.push(id); else if (!on && i >= 0) arr.splice(i, 1);
    return on;
  };
  function orderOf(scope, id, natural) {
    const i = lay(scope).order.indexOf(String(id));
    return i >= 0 ? i : 1000 + (natural || 0);            // ones the saved layout doesn't know go after, in page order
  }
  function part(scope, id, p) {
    const parts = lay(scope).parts, own = parts[id] && parts[id][p], all = parts["*"] && parts["*"][p];
    return own !== undefined ? own : all !== undefined ? all : true;
  }

  // ------------------------------------------------------------ re-render hooks, busy
  const hooks = {};
  let dragging = null;
  const rerender = (scope) => { if (scope && hooks[scope]) hooks[scope](); else Object.values(hooks).forEach((f) => f()); };
  function busy(el) {
    if (dragging) return true;
    if (!el) return false;
    if (el.querySelector("details.menu[open]")) return true;
    const a = document.activeElement;
    return !!(a && el.contains(a) && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName));
  }

  // ------------------------------------------------------------ controls (HTML strings)
  const btn = (act, scope, id, label, title, extra = "") =>
    `<button type="button" class="icon ${extra}" data-ui="${act}" data-scope="${esc(scope)}" data-id="${esc(id)}" title="${esc(title)}" aria-label="${esc(title)}">${label}</button>`;
  function editButtons(scope, id) {
    const hidden = has(lay(scope).hidden, id);
    return `<span class="ui-edit cluster" style="gap:2px">${btn("up", scope, id, "↑", "move up / left")}${btn("down", scope, id, "↓", "move down / right")}` +
      `${btn("hide", scope, id, hidden ? "◌" : "👁", hidden ? "show again" : "hide")}<span class="icon ui-handle" draggable="true" data-ui-drag="1" title="drag to move">⋮⋮</span></span>`;
  }
  function collapseButton(scope, id) {
    const c = has(lay(scope).collapsed, id);
    return btn("collapse", scope, id, "▾", c ? "expand" : "collapse", "ui-collapse").replace("<button", `<button aria-expanded="${!c}"`);
  }
  // the ⋯ menu on a card: which parts show, collapse, hide
  function cardMenu(scope, id, parts) {
    const items = (parts || []).map(([p, label]) =>
      `<button type="button" class="mi" data-ui="part" data-part="${esc(p)}" data-scope="${esc(scope)}" data-id="${esc(id)}"><span class="ck">${part(scope, id, p) ? "✓" : ""}</span>${esc(label)}</button>`).join("");
    return `<details class="menu"><summary class="icon" title="card options" aria-label="card options" role="button">⋯</summary><div class="pop">
      ${items ? `<div class="lbl">Show</div>${items}<div class="sep"></div>` : ""}
      <button type="button" class="mi" data-ui="collapse" data-scope="${esc(scope)}" data-id="${esc(id)}"><span class="ck"></span>${has(lay(scope).collapsed, id) ? "Expand" : "Collapse"}</button>
      <button type="button" class="mi" data-ui="hide" data-scope="${esc(scope)}" data-id="${esc(id)}"><span class="ck"></span>Hide this card</button>
      <button type="button" class="mi" data-ui="edit"><span class="ck"></span>Edit layout…</button></div></details>`;
  }
  // a panel header's view menu over all its cards: card size, parts for every card, collapse / expand all
  function viewMenu(scope, parts) {
    const all = (p) => { const a = lay(scope).parts["*"]; return !a || a[p] !== false; };
    const seg = Object.entries({ s: "S", m: "M", l: "L" }).map(([v, l]) =>
      `<button type="button" class="${get("cards") === v ? "on" : "ghost"}" data-ui="set" data-k="cards" data-v="${v}">${l}</button>`).join("");
    return `<details class="menu"><summary class="icon" title="view options" aria-label="view options" role="button">☰</summary><div class="pop">
      <div class="lbl">Card size</div><div class="seg">${seg}</div>
      ${(parts || []).map(([p, label]) => `<button type="button" class="mi" data-ui="partall" data-part="${esc(p)}" data-scope="${esc(scope)}"><span class="ck">${all(p) ? "✓" : ""}</span>${esc(label)} on every card</button>`).join("")}
      <button type="button" class="mi" data-ui="sort" data-scope="${esc(scope)}"><span class="ck">${lay(scope).sort !== "manual" ? "✓" : ""}</span>Sort by status (working, on, off)</button>
      <div class="sep"></div>
      <button type="button" class="mi" data-ui="collapseall" data-scope="${esc(scope)}"><span class="ck"></span>Collapse all</button>
      <button type="button" class="mi" data-ui="expandall" data-scope="${esc(scope)}"><span class="ck"></span>Expand all</button>
      <button type="button" class="mi" data-ui="showall" data-scope="${esc(scope)}"><span class="ck"></span>Show hidden cards</button></div></details>`;
  }

  // ------------------------------------------------------------ panels (static sections)
  function panels(container) {
    const scope = container.dataset.panels;
    const items = [...container.children].filter((el) => el.matches(".panel[data-panel]"));
    items.forEach((el, n) => {
      const id = el.dataset.panel, l = lay(scope), h = el.querySelector(":scope > .panel-h");
      if (h && !h.querySelector(":scope > .ui-collapse")) {
        h.insertAdjacentHTML("afterbegin", collapseButton(scope, id));
        h.insertAdjacentHTML("beforeend", editButtons(scope, id));
      }
      el.dataset.scope = scope;
      el.style.order = orderOf(scope, id, n);
      el.classList.toggle("collapsed", has(l.collapsed, id));
      el.classList.toggle("is-hidden", has(l.hidden, id));
      const cb = h && h.querySelector(":scope > .ui-collapse");
      if (cb) { cb.setAttribute("aria-expanded", String(!has(l.collapsed, id))); cb.title = has(l.collapsed, id) ? "expand" : "collapse"; }
      const hb = h && h.querySelector('[data-ui="hide"]');
      if (hb) { hb.textContent = has(l.hidden, id) ? "◌" : "👁"; hb.title = has(l.hidden, id) ? "show again" : "hide"; }
    });
  }
  const allPanels = () => document.querySelectorAll("[data-panels]").forEach(panels);

  // ------------------------------------------------------------ moving things
  function idsInOrder(scope) {
    const els = [...document.querySelectorAll(`[data-scope="${CSS.escape(scope)}"][data-panel], [data-scope="${CSS.escape(scope)}"][data-card]`)]
      .filter((el) => el.parentElement && (el.parentElement.dataset.panels === scope || el.parentElement.dataset.cards === scope));
    return els.map((el, n) => [el.dataset.panel || el.dataset.card, (+el.style.order || 0), n])
      .sort((a, b) => a[1] - b[1] || a[2] - b[2]).map((x) => x[0]);
  }
  function move(scope, id, to) {             // to: an index, or [beforeId]
    const ids = idsInOrder(scope).filter((x) => x !== id);
    let i = Array.isArray(to) ? ids.indexOf(to[0]) : to;
    if (i < 0) i = ids.length;
    ids.splice(Math.max(0, Math.min(ids.length, i)), 0, id);
    lay(scope).order = ids;
    changed(scope);
  }
  function step(scope, id, d) {
    const ids = idsInOrder(scope), i = ids.indexOf(id);
    if (i < 0) return;
    move(scope, id, Math.max(0, i + d));
  }
  function changed(scope) { save(); allPanels(); rerender(scope); }

  // ------------------------------------------------------------ settings menu + edit bar
  function settingsHTML() {
    const seg = (k) => `<div class="seg">${CHOICES[k].map(([v, l]) => `<button type="button" class="${get(k) === v ? "on" : "ghost"}" data-ui="set" data-k="${k}" data-v="${v}">${l}</button>`).join("")}</div>`;
    return `<details class="menu settings"><summary class="icon" title="Display settings" aria-label="Display settings" role="button">⚙</summary><div class="pop">
      <div class="lbl">Theme</div>${seg("theme")}<div class="lbl">Density</div>${seg("density")}
      <div class="lbl">Text size</div>${seg("text")}<div class="lbl">Card size</div>${seg("cards")}
      <div class="sep"></div>
      <button type="button" class="mi" data-ui="edit"><span class="ck">${root.classList.contains("editing") ? "✓" : ""}</span>Edit layout</button>
      <button type="button" class="mi" data-ui="resetlayout"><span class="ck"></span>Reset this page's layout</button>
      <button type="button" class="mi" data-ui="resetall"><span class="ck"></span>Reset all display settings</button>
      <div class="stamp" style="padding:4px 8px">Kept in this browser only.</div></div></details>`;
  }
  function drawSettings() {
    document.querySelectorAll("[data-settings]").forEach((el) => {
      const open = !!el.querySelector("details[open]");
      el.innerHTML = settingsHTML();
      if (open) el.querySelector("details").open = true;
    });
  }
  function setEditing(on) {
    root.classList.toggle("editing", on);
    drawSettings(); rerender();
  }

  // ------------------------------------------------------------ events (delegated, once)
  document.addEventListener("click", (e) => {
    document.querySelectorAll("details.menu[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
    const b = e.target.closest("[data-ui]");
    if (!b) return;
    const act = b.dataset.ui, scope = b.dataset.scope, id = b.dataset.id;
    const inMenu = b.closest("details.menu");
    if (act === "set") {
      S[b.dataset.k] = b.dataset.v; save(); applySettings(); drawSettings();
      document.querySelectorAll(`details.menu [data-ui="set"][data-k="${b.dataset.k}"]`).forEach((x) => { x.className = x.dataset.v === b.dataset.v ? "on" : "ghost"; });
      window.dispatchEvent(new CustomEvent("farm-ui-settings"));
      return;
    }
    if (act === "edit") { if (inMenu) inMenu.open = false; setEditing(!root.classList.contains("editing")); return; }
    if (act === "resetlayout") {
      if (!confirm("Put this page's sections and cards back in their default order, all shown and expanded?")) return;
      for (const k of Object.keys(S.L)) if (k.startsWith(page() + "/")) delete S.L[k];
      save(); allPanels(); rerender(); drawSettings(); return;
    }
    if (act === "resetall") {
      if (!confirm("Reset theme, sizes and every page's layout in this browser?")) return;
      S = { L: {} }; save(); applySettings(); allPanels(); rerender(); drawSettings(); return;
    }
    if (!scope) return;
    const l = lay(scope);
    if (act === "collapse") toggleIn(l.collapsed, id);
    else if (act === "hide") toggleIn(l.hidden, id);
    else if (act === "up") return step(scope, id, -1);
    else if (act === "down") return step(scope, id, 1);
    else if (act === "part") { (l.parts[id] = l.parts[id] || {})[b.dataset.part] = !part(scope, id, b.dataset.part); }
    else if (act === "partall") {
      const p = b.dataset.part, cur = !(l.parts["*"] && l.parts["*"][p] === false);
      (l.parts["*"] = l.parts["*"] || {})[p] = !cur;
      for (const k of Object.keys(l.parts)) if (k !== "*" && l.parts[k]) delete l.parts[k][p];   // "every card" wins
    }
    else if (act === "sort") l.sort = l.sort === "manual" ? "status" : "manual";
    else if (act === "collapseall") l.collapsed = idsInOrder(scope);
    else if (act === "expandall") l.collapsed = [];
    else if (act === "showall") l.hidden = [];
    else return;
    if (inMenu) inMenu.open = false;
    changed(scope);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll("details.menu[open]").forEach((d) => { d.open = false; });
  });

  // drag and drop: only from the ⋮⋮ handle, only in edit mode; ↑ / ↓ do the same on touch and keyboard
  const target = (el) => el && el.closest("[data-scope][data-panel], [data-scope][data-card]");
  document.addEventListener("dragstart", (e) => {
    if (!e.target.closest || !e.target.closest("[data-ui-drag]") || !root.classList.contains("editing")) return;
    const t = target(e.target);
    if (!t) return;
    dragging = { scope: t.dataset.scope, id: t.dataset.panel || t.dataset.card, el: t };
    t.classList.add("dragging");
    try { e.dataTransfer.setData("text/plain", dragging.id); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setDragImage(t, 20, 20); } catch (err) { /* old browsers */ }
  });
  document.addEventListener("dragover", (e) => {
    if (!dragging) return;
    const t = target(e.target);
    document.querySelectorAll(".drop-before").forEach((x) => x.classList.remove("drop-before"));
    if (t && t.dataset.scope === dragging.scope && t !== dragging.el) { e.preventDefault(); t.classList.add("drop-before"); }
  });
  document.addEventListener("drop", (e) => {
    if (!dragging) return;
    const t = target(e.target);
    if (t && t.dataset.scope === dragging.scope && t !== dragging.el) {
      e.preventDefault();
      const d = dragging; dragging = null;
      move(d.scope, d.id, [t.dataset.panel || t.dataset.card]);
    }
  });
  document.addEventListener("dragend", () => {
    document.querySelectorAll(".drop-before, .dragging").forEach((x) => x.classList.remove("drop-before", "dragging"));
    const was = dragging; dragging = null;
    if (was) rerender(was.scope);
  });

  // the sticky header's height, for things that stick under it (edit bar, project toolbar, NAS sidebar); 0 when the
  // header scrolls away (phones)
  function measureHead() {
    const h = document.querySelector("header.appbar");
    root.style.setProperty("--head", h && getComputedStyle(h).position === "sticky" ? h.offsetHeight + "px" : "0px");
  }
  window.addEventListener("resize", measureHead);
  window.addEventListener("farm-ui-settings", () => setTimeout(measureHead, 50));

  document.addEventListener("DOMContentLoaded", () => {
    measureHead();
    if (window.ResizeObserver && document.querySelector("header.appbar")) new ResizeObserver(measureHead).observe(document.querySelector("header.appbar"));
    drawSettings();
    allPanels();
    const main = document.querySelector("main");
    if (main && !document.querySelector(".editbar")) {
      main.insertAdjacentHTML("afterbegin", `<div class="editbar"><b>Editing layout</b><span>Drag ⋮⋮ or use ↑ ↓ to move sections and cards, 👁 hides one (hidden ones stay greyed here so you can bring them back).</span>
        <button type="button" class="mini" data-ui="edit" style="margin-left:auto">Done</button></div>`);
    }
  });

  window.farmUI = {
    settings: () => ({ theme: get("theme"), density: get("density"), text: get("text"), cards: get("cards") }),
    editing: () => root.classList.contains("editing"),
    // what a page needs while it builds a card: order (for style="order:…"), collapsed, hidden, and part(p)
    // rank: the card's status group (0 working, 1 on, 2 off) - used when the scope sorts by status
    cardState(scope, id, natural, rank) {
      const l = lay(scope), byStatus = l.sort !== "manual" && rank != null;
      return { order: (byStatus ? rank * 10000 : 0) + orderOf(scope, id, natural), collapsed: has(l.collapsed, id),
               hidden: has(l.hidden, id), part: (p) => part(scope, id, p) };
    },
    sortsByStatus: (scope) => lay(scope).sort !== "manual",
    // header controls for a card: collapse chevron, edit-mode buttons and the ⋯ menu (parts = [[key, label], ...])
    cardControls: (scope, id, parts) => collapseButton(scope, id) + editButtons(scope, id) + cardMenu(scope, id, parts),
    viewMenu, panels: allPanels, busy,
    onCards(scope, fn) { hooks[scope] = fn; },
    // a CSS colour token's current value (the 3D viewer reads --media for its clear colour)
    token: (name) => getComputedStyle(root).getPropertyValue(name).trim(),
  };
})();
