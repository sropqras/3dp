// Printers tab against tests/ui_printers.py's fake: snapshot -> stage [printer] max_velocity -> apply -> rollback.
const { chromium } = require(process.env.PLAYWRIGHT || "playwright");
const [base, shots] = process.argv.slice(2);
(async () => {
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1400, height: 1000 } });
  const errs = []; p.on("pageerror", (e) => errs.push(String(e))); p.on("dialog", (d) => d.accept());
  await p.goto(base + "/printers"); await p.waitForSelector(".card", { timeout: 30000 });
  await p.fill("#operator", "UI check"); await p.dispatchEvent("#operator", "change");
  await p.click("text=Snapshot now"); await p.waitForTimeout(800);
  await p.evaluate(() => refresh()); await p.waitForTimeout(800);
  await p.click("text=+ Stage change"); await p.waitForTimeout(800);
  await p.fill("#cd-section", "printer"); await p.fill("#cd-option", "max_velocity"); await p.fill("#cd-value", "180");
  await p.fill("#cd-reason", "UI check: slower travel");
  await p.click("text=Stage (nothing reaches the printer yet)"); await p.waitForTimeout(1200);
  console.log("staged:", await p.textContent("#cd-msg"));
  console.log("preview has +180:", (await p.textContent("#cd-preview")).includes("+max_velocity: 180"));
  await p.locator("#chgdlg").screenshot({ path: shots + "/stage.png" });
  await p.click("#chgdlg >> text=Close");
  await p.evaluate(() => refresh()); await p.waitForTimeout(1000);
  await p.click(".card >> button:text-is('Apply')"); await p.waitForTimeout(3000);
  await p.evaluate(() => refresh()); await p.waitForTimeout(1200);
  console.log("after apply:", (await p.textContent(".card details:has-text('Changes')")).replace(/\s+/g, " ").slice(0, 160));
  await p.screenshot({ path: shots + "/printers_applied.png", fullPage: true });
  await p.click(".card >> button:text-is('Rollback')"); await p.waitForTimeout(3000);
  await p.evaluate(() => refresh()); await p.waitForTimeout(1200);
  console.log("after rollback:", (await p.textContent(".card details:has-text('Changes')")).replace(/\s+/g, " ").slice(0, 120));
  // Stage fix from the audit table (the fake's config fails real rules), then apply it
  let alertText = ""; p.removeAllListeners("dialog"); p.on("dialog", (d) => { alertText = d.message(); d.accept(); });
  await p.click(".card details summary:has-text('rules')");
  const rules = await p.$$eval(".card details table tr", (rs) => rs.filter((r) => r.querySelector("button")).map((r) => r.children[1].textContent));
  console.log("rules with a Stage fix button:", rules.length, rules.slice(0, 4));
  await p.click(".card details table button:has-text('Stage fix') >> nth=0"); await p.waitForTimeout(1500);
  console.log("stage fix:", alertText.slice(0, 140));
  console.log("page errors:", errs.length ? errs : "none");
  await b.close();
})();
