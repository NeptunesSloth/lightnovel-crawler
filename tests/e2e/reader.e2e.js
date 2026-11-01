// Browser tests for the Reader (/reader). Run through run_reader_e2e.py, which
// seeds the library and starts the server; BASE, TOKEN and IDS come from it.
// Anything needing the internet is answered here with page.route().
const { chromium } = require("playwright");

const BASE = process.env.BASE;
const TOKEN = process.env.TOKEN;
const IDS = JSON.parse(process.env.IDS || "{}");
const READER = BASE + "/reader";

let failures = 0;
function check(name, ok, detail) {
  console.log((ok ? "  ok   " : "  FAIL ") + name + (ok ? "" : "  — " + JSON.stringify(detail)));
  if (!ok) failures++;
}

async function open(browser, storage) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const page = await context.newPage();
  page.errors = [];
  page.on("pageerror", (e) => page.errors.push(e.message));
  await page.goto(READER);
  await page.evaluate(([t, s]) => {
    localStorage.setItem("lncrawl_token", t);
    (s || []).forEach(([k, v]) => localStorage.setItem(k, v));
  }, [TOKEN, storage]);
  await page.goto(READER);
  await page.waitForFunction(() => /novel\(s\)/.test(document.getElementById("lib-status").textContent));
  await page.waitForTimeout(800);
  return page;
}

const titles = (page) =>
  page.$$eval("#lib-list .libcard h3", (hs) =>
    hs.map((h) => [...h.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim()));
const status = (page) => page.textContent("#lib-status");
async function search(page, q) { await page.fill("#search", q); await page.waitForTimeout(300); }
async function showFilters(page) {
  if (await page.isHidden("#lib-filters")) await page.click("#lib-filters-btn");
}
async function clearFilters(page) {
  await page.evaluate(() => document.getElementById("lib-clear").click());
  await page.waitForTimeout(200);
}

(async () => {
  const browser = await chromium.launch();
  const older = Date.now() - 2.5 * 3600 * 1000;
  const A = await open(browser, [["lncrawl_reader_pos", JSON.stringify({
    [IDS["Reading Now"]]: { id: "x", serial: 3, ts: older, scroll: 0.2 },
  })]]);

  console.log("Library");
  check("cards are drawn in batches", (await A.$$eval("#lib-list .libcard", (c) => c.length)) === 60);
  await A.evaluate(() => window.scrollTo(0, document.body.scrollHeight)); await A.waitForTimeout(600);
  check("the rest appear on scrolling", (await A.$$eval("#lib-list .libcard", (c) => c.length)) === 76);
  await A.evaluate(() => window.scrollTo(0, 0));
  check("filters start folded away", await A.isHidden("#lib-filters"));
  await A.click("#lib-filters-btn");
  check("the Filters button opens them", await A.isVisible("#lib-filters"));

  console.log("Search");
  await search(A, "solo leveling ragnarok");
  check("punctuation is ignored", (await titles(A)).includes("Solo Leveling: Ragnarok"));
  await search(A, "sololeveling");
  check("an alternative title is searched", (await titles(A)).includes("Na Honjaman Level-Up"));
  await search(A, "chugong");
  check("authors are searched", JSON.stringify(await titles(A)) === '["Na Honjaman Level-Up"]');
  await search(A, "omnicsient reder");
  check("a typo still finds it", (await titles(A)).includes("Omniscient Reader") && /close to/.test(await status(A)));
  await search(A, "zzzz");
  check("a miss offers to search the sources", await A.isVisible("#lib-web-btn"));
  // the "sources in my library" search sends only those domains
  let sent = null, polls = 0;
  await A.route("**/api/job/create/search-sources", (r) => { sent = JSON.parse(r.request().postData()); r.fulfill({ json: { id: "job1", status: 0, extra: {} } }); });
  await A.route("**/api/job/job1", (r) => { polls++; r.fulfill({ json: { id: "job1", status: polls > 1 ? 2 : 1, done: 1, total: 1, extra: { search_results: [
    { title: "Zzzz Novel", url: "https://x.org/zzzz", info: "Ch 3" },
    { title: "Omniscient Reader", url: "https://c.com/n/4", info: "" }] } } }); });
  await A.click("#lib-web-btn"); await A.waitForTimeout(4500);
  check("library sources only are searched", sent && sent.results_only && Array.isArray(sent.domains) && sent.domains.includes("a.com") && !sent.domain, sent);
  const rows = await A.$$eval("#lib-web-list .web-row", (r) => r.map((x) => x.innerText));
  check("results list, owned ones marked", rows.length === 2 && /In library/.test(rows[1]),
    { rows, polls, note: await A.textContent("#lib-web-status"), hidden: await A.isHidden("#lib-web") });
  await A.click("#lib-web-stop").catch(() => {});
  await search(A, "");

  console.log("Filters, tags and views");
  const chips = await A.$$eval("#lib-cats .chip", (cs) => cs.map((c) => c.textContent));
  check("tags spelled differently are merged", chips.includes("Boys' Love") && !chips.includes("BL"), chips);
  await A.fill("#lib-tag", "science fiction"); await A.waitForTimeout(300);
  check("a merged tag finds both spellings", JSON.stringify((await titles(A)).sort()) === '["Omniscient Reader","Reading Now"]');
  check("the Filters button counts what is on", /\(1\)/.test(await A.textContent("#lib-filters-btn")));
  await clearFilters(A);
  await A.check("#lib-dupes");
  check("possible duplicates sit together", (await titles(A)).length === 2);
  await A.uncheck("#lib-dupes");
  await A.check("#lib-nobl");
  check("Hide BL hides both spellings", !(await titles(A)).some((t) => /Beginning/.test(t)));
  await A.uncheck("#lib-nobl");
  await A.selectOption("#lib-state", "completed"); await A.selectOption("#lib-sort", "title");
  A.once("dialog", (d) => d.accept("Done A-Z"));
  await A.click("#lib-view-save");
  await clearFilters(A); await A.selectOption("#lib-sort", "recent");
  await A.selectOption("#lib-views", "Done A-Z");
  check("a saved view comes back", JSON.stringify(await titles(A)) === '["Na Honjaman Level-Up","Omniscient Reader"]');
  await A.selectOption("#lib-views", ""); await clearFilters(A);

  console.log("New chapters, novel page and progress");
  await search(A, "reading now");
  check("new chapters since you read are flagged", (await A.$eval("#lib-list .libcard", (c) => c.innerText)).includes("new chapters"));
  await A.click("#lib-list .libcard >> nth=0"); await A.waitForSelector(".chap");
  const marks = await A.$$eval(".chap", (cs) => cs.map((c) => c.querySelector(".rd").textContent));
  check("chapters read are ticked and yours marked", JSON.stringify(marks) === '["✓","✓","▶","",""]', marks);
  await A.click("#rate-box span >> nth=3");
  check("a rating is kept", (await A.evaluate((id) => JSON.parse(localStorage.getItem("lncrawl_reader_ratings"))[id], IDS["Reading Now"])) === 4);
  await A.click("#prog-done"); await A.waitForTimeout(1000);
  const pos = await A.evaluate((id) => JSON.parse(localStorage.getItem("lncrawl_reader_pos"))[id], IDS["Reading Now"]);
  check("Mark finished goes to the end", pos.serial === 5 && pos.scroll === 1, pos);
  await A.click("#shelf-btn");
  A.once("dialog", (d) => d.accept("Plan to read"));
  await A.click("#shelf-box .chip:has-text('New shelf')"); await A.waitForTimeout(1500);
  check("a shelf is made with the novel on it", /Plan to read/.test(await A.textContent("#shelf-btn")));
  A.once("dialog", (d) => d.accept("1"));
  await A.click("#prog-set"); await A.waitForSelector(".chap");
  A.once("dialog", (d) => d.accept("all"));
  await A.click("#offline-btn");
  await A.waitForFunction(() => /saved on this device/.test(document.getElementById("offline-msg").textContent), null, { timeout: 20000 });
  const cached = await A.evaluate(async () => (await (await caches.open("lncrawl-reader-v1")).keys()).filter((r) => /\/read\?auto_fetch=false/.test(r.url)).length);
  check("chapters are kept for offline", cached === 5, cached);
  await A.click(".chap >> nth=0"); await A.waitForTimeout(800);
  await A.click("#nav-back"); await A.click("#nav-back").catch(() => {});
  await A.waitForFunction(() => /novel\(s\)/.test(document.getElementById("lib-status").textContent));
  await search(A, "");

  console.log("Shelves, stats, backup");
  await A.click("#lib-more summary"); await A.click("#lib-shelves-btn");
  check("shelves are listed to manage", /Plan to read \(1\)/.test(await A.textContent("#lib-shelves-list")));
  A.once("dialog", (d) => d.accept("Reading list"));
  await A.click("#lib-shelves-list button:has-text('Rename')"); await A.waitForTimeout(1200);
  check("a shelf can be renamed", /Reading list/.test(await A.textContent("#lib-shelves-list")));
  await A.click("#lib-more summary"); await A.click("#lib-stats-btn");
  check("stats count the chapter opened", /This week: 1 chapters/.test(await A.textContent("#lib-stats-body")));
  await A.click("#lib-more summary");
  const [download] = await Promise.all([A.waitForEvent("download"), A.click("#lib-backup")]);
  const backup = JSON.parse(require("fs").readFileSync(await download.path(), "utf8"));
  check("a backup holds state and shelves", backup.app === "lncrawl-reader" && backup.shelves[0].name === "Reading list" && backup.state.rateitems);

  console.log("Sync between devices");
  async function star(page, title) {
    await search(page, title);
    await page.click(`#lib-list .libcard:has-text("${title}") .fav-star`);
    await search(page, "");
  }
  await star(A, "Omniscient Reader");
  await A.waitForTimeout(3500);
  const B = await open(browser);
  await B.waitForTimeout(1500);
  const bFavs = await B.evaluate(() => JSON.parse(localStorage.getItem("lncrawl_reader_favs") || "[]"));
  check("a favorite reaches another device", bFavs.includes(IDS["Omniscient Reader"]), bFavs);
  // both change different favorites without seeing each other: neither is lost
  await star(B, "Filler 01");
  await star(A, "Filler 02");
  await A.waitForTimeout(3500); await B.waitForTimeout(500);
  await A.reload(); await A.waitForTimeout(2500);
  const aFavs = await A.evaluate(() => JSON.parse(localStorage.getItem("lncrawl_reader_favs") || "[]"));
  check("changes from both devices survive", [IDS["Filler 01"], IDS["Filler 02"], IDS["Omniscient Reader"]].every((id) => aFavs.includes(id)), aFavs);

  console.log("Phone");
  await A.setViewportSize({ width: 390, height: 800 }); await A.waitForTimeout(300);
  const overflow = await A.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check("nothing runs off the side on a phone", overflow <= 1, overflow);

  check("no errors in the page", A.errors.length === 0 && B.errors.length === 0, A.errors.concat(B.errors));
  await browser.close();
  console.log(failures ? failures + " check(s) failed" : "all checks passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
