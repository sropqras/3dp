// Printers tab against tests/ui_printers.py's fake: snapshot -> stage [printer] max_velocity -> apply -> rollback.
const { chromium } = require(process.env.PLAYWRIGHT || "playwright");
const [base, shots] = process.argv.slice(2);
(async () => {
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1400, height: 1000 } });
  const errs = []; p.on("pageerror", (e) => errs.push(String(e))); p.on("dialog", (d) => d.accept());
  await p.goto(base + "/printers"); await p.waitForSelector(".card", { timeout: 30000 });
  await p.evaluate(() => farmOperatorSet("UI check"));     // the header's "You" link (static/operator.js)
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
  // Onboard the second instance: camera, then the registry form (the fake's bed is whatever we type)
  await p.evaluate(() => refresh()); await p.waitForTimeout(1500);
  const nb = p.locator(".card:has(details.onboard)");
  await nb.locator("details.onboard summary").click();
  console.log("checklist:", (await nb.locator("details.onboard summary").textContent()).trim());
  await nb.locator("details.onboard input[placeholder='webcam3']").fill("webcam9");
  await nb.locator("details.onboard button:has-text('Register')").click(); await p.waitForTimeout(1200);
  console.log("camera:", alertText);
  await p.evaluate(() => refresh()); await p.waitForTimeout(1500);
  const form = p.locator(".card:has(details.onboard) details.onboard form");
  await p.$eval(".card:has(details.onboard) details.onboard", (d) => { d.open = true; });   // a refresh keeps it open now
  await form.locator("input[name=x1]").fill("300"); await form.locator("input[name=y1]").fill("300"); await form.locator("input[name=z]").fill("300");
  await form.locator("input[value='neo-petg-036']").check();
  await p.screenshot({ path: shots + "/onboard.png", fullPage: true });
  await form.locator("button:has-text('Add to the farm')").click(); await p.waitForTimeout(1500);
  console.log("onboard:", alertText.slice(0, 120));
  await p.evaluate(() => refresh()); await p.waitForTimeout(1500);
  console.log("onboarded cards:", await p.$$eval(".card .name", (ns) => ns.map((n) => n.textContent)),
              "still not onboarded:", await p.locator("details.onboard").count());
  const fs = p.locator(".card:has(.name:text-is('newone')) details:has-text('Farm settings')");
  await fs.locator("summary").click(); await fs.locator("input[name=readonly]").check();
  await fs.locator("button:has-text('Save')").click(); await p.waitForTimeout(1200);
  console.log("settings:", alertText);
  await p.evaluate(() => refresh()); await p.waitForTimeout(1500);
  console.log("newone badge:", await p.textContent(".card:has(.name:text-is('newone')) .ph"));
  console.log("page errors:", errs.length ? errs : "none");
  await b.close();
})();
