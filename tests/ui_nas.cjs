// Read-only browser check of the NAS catalogue against a running Farm Control (live data is fine: it only reads,
// apart from thumbnails being built into the cache).  PLAYWRIGHT=<path> node tests/ui_nas.cjs http://127.0.0.1:10111 <shots-dir>
const { chromium } = require(process.env.PLAYWRIGHT);
(async () => {
  const b = await chromium.launch(); const pg = await b.newPage({ viewport: { width: 1400, height: 1000 } });
  const errs = [], miss = [];
  pg.on("pageerror", (e) => errs.push(e.message));
  pg.on("console", (m) => { if (m.type() === "error") (/404/.test(m.text()) ? miss : errs).push(m.text()); });
  await pg.addInitScript(() => { try { localStorage.removeItem("nasState"); } catch (e) {} });
  await pg.goto(process.argv[2] + "/#inventory"); await pg.waitForTimeout(1500);
  await pg.click("#iv-v-nas"); await pg.waitForSelector(".ncard", { timeout: 30000 });
  const bar = async () => (await pg.locator(".nas-bar").innerText()).replace(/\s+/g, " ").slice(0, 140);
  console.log("cards:", await pg.locator(".ncard").count(), "| bar:", await bar());
  await pg.waitForTimeout(5000);
  await pg.screenshot({ path: process.argv[3] + "/nas_grid.png" });
  await pg.click(".nas-side .seg button:nth-child(2)"); await pg.waitForSelector(".ncat");
  await pg.click(".ncat >> nth=0"); await pg.waitForFunction(() => NS.total < 1668);
  console.log("category 1:", await bar());
  const before = await pg.evaluate(() => NS.total); await pg.click(".facets .chip.f:has-text('.3mf')"); await pg.waitForFunction((b) => NS.total !== b, before);
  console.log("+.3mf chip:", await bar());
  await pg.click(".nas-bar .seg button:has-text('Table')"); await pg.waitForSelector(".ntable");
  console.log("table rows:", await pg.locator(".ntable tr").count());
  await pg.click(".nas-bar .seg button:has-text('Grid')"); await pg.waitForSelector(".ncard");
  await pg.click(".ncard >> nth=0 >> .meta"); await pg.waitForSelector("#nas-pack[open]", { timeout: 20000 });
  console.log("drawer:", (await pg.locator("#np-body").innerText()).slice(0, 200).replace(/\s+/g, " "));
  await pg.screenshot({ path: process.argv[3] + "/nas_pack.png" });
  await pg.click("#np-body >> text=3D preview"); await pg.waitForTimeout(8000);
  console.log("viewer:", await pg.locator("#v-file-note").innerText(), "| msg:", await pg.locator("#v-msg").innerText().catch(() => ""));
  await pg.screenshot({ path: process.argv[3] + "/nas_3d.png" });
  console.log("page errors:", errs.length ? errs : "none", "| thumbnails queued (404 then retried):", miss.length);
  await b.close();
})();
