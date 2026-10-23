// Who is using Farm Control: the name recorded with every action (change log, marks, applies). The server's
// FARM_OPERATOR is the default; the "You" link in the header changes it for this browser only.
// The server fills in __DEFAULT__ when it serves this file.
const FARM_OPERATOR = __DEFAULT__;
function farmOperator() {
  try { return (localStorage.getItem("operator") || "").trim() || FARM_OPERATOR; } catch (e) { return FARM_OPERATOR; }
}
function farmOperatorSet(name) {
  name = String(name || "").trim().slice(0, 40);
  try { if (!name || name === FARM_OPERATOR) localStorage.removeItem("operator"); else localStorage.setItem("operator", name); } catch (e) {}
  document.querySelectorAll(".you-name").forEach((el) => (el.textContent = farmOperator()));
}
function farmOperatorEdit() {
  let d = document.getElementById("youdlg");
  if (!d) {
    d = document.createElement("dialog");
    d.id = "youdlg";
    d.style.cssText = "width:min(360px,calc(100vw - 32px));padding:18px;border:1px solid var(--line,#333);border-radius:12px;background:var(--panel,#111);color:var(--text,#eee)";
    d.innerHTML = `<form method="dialog" style="display:grid;gap:10px">
        <b>Who is using Farm Control?</b>
        <div style="font-size:12px;opacity:.75">Recorded with everything you do here (applies, marks, edits). Only this browser changes.</div>
        <input id="you-input" maxlength="40" autocomplete="off" style="padding:8px">
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button value="reset" class="ghost" type="submit">Use default</button>
          <button value="cancel" class="ghost" type="submit">Cancel</button>
          <button value="save" type="submit">Save</button></div></form>`;
    document.body.appendChild(d);
    d.addEventListener("close", () => {
      if (d.returnValue === "save") farmOperatorSet(document.getElementById("you-input").value);
      if (d.returnValue === "reset") farmOperatorSet("");
    });
  }
  document.getElementById("you-input").value = farmOperator();
  d.returnValue = "";
  d.showModal();
  document.getElementById("you-input").select();
}
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-you]").forEach((el) => {
    el.innerHTML = `You: <a href="#" class="you-name" title="Change who is recorded with your actions" style="color:inherit;text-decoration:underline dotted"></a> ✎`;
    el.querySelector("a").addEventListener("click", (e) => { e.preventDefault(); farmOperatorEdit(); });
  });
  farmOperatorSet(farmOperator());
});
