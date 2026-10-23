// Drives tests/ui_check.py's throwaway Farm Control in headless Chromium: 3D -> Approve; Start dialog -> start.
// usage: node tests/ui_check.cjs <base-url> <review-id> <queued-id> <shots-dir>   (needs playwright resolvable)
const { chromium } = require(process.env.PLAYWRIGHT || "playwright");
const [base, reviewId, queuedId, shots] = process.argv.slice(2);
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1400, height: 1000 } });
  const errs = []; p.on("pageerror", (e) => errs.push(String(e)));
  p.on("dialog", (d) => d.accept(d.type() === "prompt" ? "test" : undefined));
  await p.goto(base); await p.waitForSelector("#builds tr td", { timeout: 30000 });
  const row = (id) => p.locator("#builds tr", { has: p.locator(`td:first-child:text-is("${id}")`) });
  console.log("review row before view:", (await row(reviewId).innerText()).replace(/\s+/g, " "));
  await row(reviewId).locator("button", { hasText: "3D" }).click();
  await p.waitForFunction(() => getComputedStyle(document.getElementById("v-msg")).display === "none", null, { timeout: 15000 });
  await p.locator("#viewer button", { hasText: "Close" }).click();
  await p.evaluate(() => refresh()); await p.waitForTimeout(800);
  await row(reviewId).locator("button", { hasText: "Approve" }).click();
  await p.waitForTimeout(1200);
  console.log("review row after approve:", (await row(reviewId).innerText()).replace(/\s+/g, " "));
  await p.locator("#builds").screenshot({ path: `${shots}/builds.png` });
  await row(queuedId).locator("button", { hasText: "Start…" }).click();
  await p.waitForFunction(() => document.getElementById("sd-msg").textContent !== "checking…", null, { timeout: 15000 });
  console.log("start dialog reasons:", JSON.stringify(await p.$$eval("#sd-reasons li", (l) => l.map((x) => x.textContent))));
  for (const id of ["sd-viewed", "sd-bed", "sd-colour"]) await p.check(`#${id}`);
  const spools = await p.$$eval("#sd-spool option", (o) => o.map((x) => x.value).filter(Boolean));
  await p.selectOption("#sd-spool", spools.find((s) => s) || "");
  await p.fill("#sd-operator", "UI check");
  await p.locator("#startdlg").screenshot({ path: `${shots}/start.png` });
  await p.click("#sd-go"); await p.waitForTimeout(1500);
  console.log("dialog open after start:", await p.evaluate(() => document.getElementById("startdlg").open),
              "reasons:", JSON.stringify(await p.$$eval("#sd-reasons li", (l) => l.map((x) => x.textContent))));
  console.log("page errors:", errs.length ? errs : "none");
  await b.close();
})();
