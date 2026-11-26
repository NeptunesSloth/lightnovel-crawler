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
  // The Reader's service worker answers its fetches itself once it takes
  // control, and page.route() never sees those requests; how soon it takes
  // control is a race, so a mocked job poll could slip past to the server.
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 }, acceptDownloads: true, serviceWorkers: "block",
  });
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
  await A.route(/\/api\/job\/job1(\?.*)?$/, (r) => { polls++; r.fulfill({ json: { id: "job1", status: polls > 1 ? 2 : 1, done: 1, total: 1, extra: { search_results: [
    { title: "Zzzz Novel", url: "https://x.org/zzzz", info: "Ch 3" },
    { title: "Omniscient Reader", url: "https://c.com/n/4", info: "" }] } } }); });
  await A.click("#lib-web-btn");
  // the first poll can queue behind cover images, so wait for the result, not a clock
  await A.waitForFunction(() => document.querySelectorAll("#lib-web-list .web-row").length === 2, null, { timeout: 20000 }).catch(() => {});
  check("library sources only are searched", sent && sent.results_only && Array.isArray(sent.domains) && sent.domains.includes("a.com") && !sent.domain, sent);
  const rows = await A.$$eval("#lib-web-list .web-row", (r) => r.map((x) => x.innerText));
  check("results list, owned ones marked", rows.length === 2 && /In library/.test(rows[1]),
    { rows, polls, note: await A.textContent("#lib-web-status"), hidden: await A.isHidden("#lib-web") });
  await A.click("#lib-web-stop").catch(() => {});
  // a search whose job was deleted meanwhile ends, instead of being asked after for ever
  let gonePolls = 0;
  await A.unroute("**/api/job/create/search-sources");
  await A.route("**/api/job/create/search-sources", (r) => r.fulfill({ json: { id: "gone1", status: 0, extra: {} } }));
  await A.route(/\/api\/job\/gone1(\?.*)?$/, (r) => { gonePolls++; r.fulfill({ status: 404, json: { error: "No such job" } }); });
  await search(A, "zzzy");
  await A.click("#lib-web-btn");
  await A.waitForFunction(() => /no longer there/.test(document.getElementById("lib-web-status").textContent), null, { timeout: 15000 }).catch(() => {});
  await A.waitForTimeout(5000);
  check("a search whose job is gone says so and stops asking",
    /no longer there/.test(await A.textContent("#lib-web-status")) && gonePolls === 1,
    { gonePolls, note: await A.textContent("#lib-web-status") });
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

  console.log("Novels with only their details");
  await A.check("#lib-noch"); await A.waitForTimeout(300);
  check("No chapters yet leaves out a downloaded novel", /75 novel/.test(await status(A)) && !(await titles(A)).includes("Reading Now"), await status(A));
  await A.uncheck("#lib-noch");
  await A.selectOption("#lib-dl", "started"); await A.waitForTimeout(300);
  check("Hide details-only leaves just the downloaded novel", JSON.stringify(await titles(A)) === '["Reading Now"]', await titles(A));
  await A.selectOption("#lib-dl", "complete"); await A.waitForTimeout(300);
  check("Fully downloaded shows a novel with every chapter", JSON.stringify(await titles(A)) === '["Reading Now"]', await titles(A));
  await A.selectOption("#lib-dl", "partial"); await A.waitForTimeout(300);
  check("Partly downloaded shows none here", /No matches/.test(await status(A)), await status(A));
  await A.selectOption("#lib-dl", "none"); await A.waitForTimeout(300);
  check("Details only leaves out the downloaded novel", /75 novel/.test(await status(A)), await status(A));
  await A.selectOption("#lib-dl", "");
  check("the library starts as covers", await A.$eval("#lib-list", (l) => l.classList.contains("grid")));
  await A.click("#lib-layout");
  check("it can be a list instead", !(await A.$eval("#lib-list", (l) => l.classList.contains("grid"))));
  await A.click("#lib-layout");
  await search(A, "omniscient");
  check("a details-only novel is marked", /details only/.test(await A.$eval("#lib-list .libcard", (c) => c.innerText)));
  let fetchBody = null;
  await A.route("**/api/job/create/bulk-download", (r) => { fetchBody = JSON.parse(r.request().postData()); r.fulfill({ json: [{ id: "job2", status: 2, extra: {} }] }); });
  await A.route(/\/api\/job\/job2(\?.*)?$/, (r) => r.fulfill({ json: { id: "job2", status: 2, extra: {} } }));
  await A.click("#lib-more summary"); await A.click("#lib-select");
  await A.click("#lib-list .libcard >> nth=0");
  A.once("dialog", (d) => d.accept());
  await A.click("#lib-getsel"); await A.waitForTimeout(600);
  check("selected novels get their chapters", fetchBody && fetchBody.urls.length === 1 && /c\.com\/n\/4/.test(fetchBody.urls[0]), fetchBody);
  check("select mode ends after starting it", await A.isHidden("#lib-getsel"));
  fetchBody = null;
  await A.click("#lib-list .libcard >> nth=0"); await A.waitForSelector("#getch-btn:not(.hidden)");
  A.once("dialog", (d) => d.accept());
  await A.click("#getch-btn"); await A.waitForTimeout(600);
  check("the novel page downloads its chapters", fetchBody && fetchBody.urls.length === 1, fetchBody);
  await A.click("#nav-back");
  await A.waitForFunction(() => /novel\(s\)/.test(document.getElementById("lib-status").textContent));
  await A.click("#lib-more summary");
  check("More offers every details-only novel", /75 details-only/.test(await A.textContent("#lib-getch")), await A.textContent("#lib-getch"));
  await A.click("#lib-more summary");
  await A.unroute("**/api/job/create/bulk-download");
  await search(A, "");

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

  console.log("Tools: download a whole site");
  const T = await browser.newPage();
  T.errors = [];
  T.on("pageerror", (e) => T.errors.push(e.message));
  await T.goto(BASE + "/tools?authToken=" + encodeURIComponent(TOKEN));
  await T.waitForTimeout(800);
  let started = null;
  await T.route("**/api/job/create/export-source", (r) => { started = JSON.parse(r.request().postData()); r.fulfill({ json: { id: "job3", status: 0, extra: {} } }); });
  await T.route(/\/api\/job\/job3(\?.*)?$/, (r) => r.fulfill({ json: { id: "job3", status: 1, done: 0, total: 1, extra: { domain: "a.com", phase: "cooling-down", cool_reason: "stalled", resume_at: Date.now() + 300000 } } }));
  check("the e-book format stays out of the way", await T.isHidden("#exp-zip-opts"));
  check("advanced options are folded away", await T.isHidden("#exp-retries"));
  await T.fill("#exp-url", "https://a.com/");
  await T.click("#export-btn"); await T.waitForTimeout(1500);
  check("it starts as an until-done library download", started && started.library_only === true && started.until_done === true, started);
  check("a pause says why and until when", /Pausing — downloads stopped responding.* at /.test(await T.textContent("body")));
  await T.selectOption("#exp-make", "zip");
  check("choosing e-books shows the format", await T.isVisible("#exp-zip-opts"));
  check("no errors on the Tools page", T.errors.length === 0, T.errors);
  await T.close();

  console.log("Downloads page");
  const D = await browser.newPage({ viewport: { width: 390, height: 900 } });
  D.errors = [];
  D.on("pageerror", (e) => D.errors.push(e.message));
  const now = Date.now();
  let jobs = [
    { id: "r1", type: 53, status: "RUNNING", title: "Download 2 novel(s) → a.com", done: 3, total: 10, is_done: false,
      created_at: now - 7200e3, started_at: now - 7000e3, updated_at: now - 5e3,
      extra: { domain: "a.com", total_novels: 10, done_novels: 3, phase: "downloading", requests_per_sec: 1.5, chapters_per_min: 42,
        eta_seconds: 5400, active_novels: [{ title: "Omniscient Reader", done: 120, total: 551 }, { title: "Solo Leveling", done: 3, total: 270 }] } },
    { id: "w1", type: 53, status: "RUNNING", title: "Export all novels → b.com", done: 1, total: 4, is_done: false,
      created_at: now - 3600e3, updated_at: now - 30e3,
      extra: { domain: "b.com", total_novels: 4, done_novels: 1, phase: "retry-waiting", quiet_rounds: 2, resume_at: now + 3600e3 } },
    { id: "p1", type: 53, status: "PAUSED", title: "Download all novels → c.com", done: 0, total: 1, is_done: false,
      created_at: now - 600e3, updated_at: now - 60e3, extra: { domain: "c.com" } },
    { id: "f1", type: 53, status: "FAILED", title: "Export all novels → d.com", done: 0, total: 1, is_done: true,
      created_at: now - 90000e3, started_at: now - 90000e3, finished_at: now - 86000e3, updated_at: now - 86000e3,
      error: "Traceback...\nDiscovery found no novels on 'd.com'", extra: { domain: "d.com" } },
    { id: "s1", type: 53, status: "SUCCESS", title: "Download 40 novel(s) → e.com", done: 40, total: 40, is_done: true,
      created_at: now - 50000e3, started_at: now - 50000e3, finished_at: now - 40000e3, updated_at: now - 40000e3,
      extra: { domain: "e.com", library_only: true, complete: 38, incomplete: 2, total_novels: 40 } },
  ];
  const calls = [];
  await D.route("**/api/jobs/downloads", (r) => r.fulfill({ json: jobs }));
  await D.route(/\/api\/job\/\w+\/(pause|resume|cancel|replay)$/, (r) => {
    calls.push(r.request().url().split("/api/job/")[1]);
    if (/p1\/resume/.test(r.request().url())) jobs = jobs.map((j) => j.id === "p1" ? Object.assign({}, j, { status: "PENDING" }) : j);
    r.fulfill({ json: true });
  });
  await D.goto(BASE + "/reader"); await D.evaluate((t) => localStorage.setItem("lncrawl_token", t), TOKEN);
  await D.goto(BASE + "/downloads"); await D.waitForSelector(".card[data-id='r1']");
  const text = (id) => D.textContent(`.card[data-id='${id}']`);
  check("the summary counts what is going on", /1 downloading/.test(await D.textContent("#summary")) && /1 paused/.test(await D.textContent("#summary")), await D.textContent("#summary"));
  const r1 = await text("r1");
  check("a running download shows both novels, speed and time left", /Omniscient Reader — chapter 120 of 551/.test(r1) && /Solo Leveling/.test(r1) && /42 chapters\/min/.test(r1) && /1h 30m left/.test(r1) && /3 of 10 novels/.test(r1), r1);
  check("a resting one says why and when it carries on", /got nothing new.*carries on at/.test(await text("w1")));
  check("a failed one shows the reason, not the traceback", /Discovery found no novels/.test(await text("f1")) && !/Traceback/.test(await text("f1")));
  check("a finished library download offers Read and Retry for partials", /2 partial/.test(await text("s1")) && /Read/.test(await text("s1")) && /Retry/.test(await text("s1")));
  await D.click(".card[data-id='r1'] button:has-text('Pause')");
  await D.click(".card[data-id='p1'] button:has-text('Resume')"); await D.waitForTimeout(800);
  check("pause and resume reach the server", calls.includes("r1/pause") && calls.includes("p1/resume"), calls);
  check("a resumed one shows as queued", /Queued/.test(await text("p1")));
  D.once("dialog", (d) => d.accept());
  await D.click(".card[data-id='w1'] button:has-text('Stop')"); await D.waitForTimeout(500);
  check("stop asks first, then stops", calls.includes("w1/cancel"), calls);
  await D.uncheck("#show-done");
  check("finished ones can be hidden", await D.isHidden(".card[data-id='s1']"));
  const dOverflow = await D.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check("the Downloads page fits a phone", dOverflow <= 1, dOverflow);
  check("no errors on the Downloads page", D.errors.length === 0, D.errors);
  await D.close();

  console.log("Books on a drive");
  const N = await open(browser);
  await N.evaluate(() => document.getElementById("lib-select").click());
  check("Keep on this computer is there in Select mode without a drive", await N.isVisible("#lib-here"));
  let told = "";
  N.once("dialog", (d) => { told = d.message(); d.accept(); });
  await N.click("#lib-here"); await N.waitForTimeout(200);
  check("without a drive it says how to set one up", /Books on a drive/.test(told), told);
  await N.close();
  const K = await open(browser);
  let moved = null;
  await K.route("**/api/books", (r) => r.fulfill({ json: {
    configured: true, plugged: false, folder: "F:\\Novels", on_drive: [IDS["Omniscient Reader"]], moving: {} } }));
  await K.reload();
  await K.waitForFunction(() => /novel\(s\)/.test(document.getElementById("lib-status").textContent));
  await K.waitForTimeout(600);
  await search(K, "omniscient");
  const away = await K.$eval("#lib-list .libcard", (c) => [c.classList.contains("away"), c.innerText]);
  check("a novel on an unplugged drive is marked", away[0] && /on the drive/.test(away[1]), away);
  await search(K, "reading now");
  check("one on this computer is not", !(await K.$eval("#lib-list .libcard", (c) => c.classList.contains("away"))));
  await K.unroute("**/api/books");
  await K.route("**/api/books", (r) => r.fulfill({ json: {
    configured: true, plugged: true, folder: "F:\\Novels", on_drive: [IDS["Omniscient Reader"]], moving: {} } }));
  await K.route("**/api/books/move", (r) => { moved = JSON.parse(r.request().postData());
    r.fulfill({ json: { configured: true, plugged: true, on_drive: [], moving: { running: false, total: 1 } } }); });
  await K.reload();
  await K.waitForFunction(() => /novel\(s\)/.test(document.getElementById("lib-status").textContent));
  await K.waitForTimeout(600);
  await search(K, "omniscient");
  await K.evaluate(() => document.getElementById("lib-select").click());
  check("selecting offers to keep books on this computer", await K.isVisible("#lib-here"));
  await K.click("#lib-list .libcard");
  K.once("dialog", (d) => d.accept());
  await K.click("#lib-here"); await K.waitForTimeout(400);
  check("keeping one here asks for its books", moved && moved.to === "here" && moved.ids[0] === IDS["Omniscient Reader"], moved);
  check("no errors with books on a drive", K.errors.length === 0, K.errors);
  await K.close();

  console.log("An e-book for the phone");
  const E = await open(browser);
  let made = null;
  await E.route(/\/api\/novel\/[^/]+\/ebook$/, (r) => { made = r.request().method();
    r.fulfill({ json: { id: "a1", file_name: "Reading Now.epub", output_file: "novels/x/artifacts/Reading Now.epub" } }); });
  await E.route("**/static/**", (r) => r.fulfill({ body: "PK epub", contentType: "application/epub+zip" }));
  await search(E, "reading now");
  await E.click("#lib-list .libcard h3"); await E.waitForTimeout(600);
  const ebookDownload = E.waitForEvent("download", { timeout: 5000 }).catch(() => null);
  await E.click("#ebook-btn");
  const file = await ebookDownload;
  check("Download e-book makes it and saves the file", made === "POST" && file && file.suggestedFilename() === "Reading Now.epub",
    [made, file && file.suggestedFilename()]);
  check("it says where it went", /Saved Reading Now\.epub/.test(await E.textContent("#offline-msg")), await E.textContent("#offline-msg"));
  check("a short chapter list has no Go to chapter", await E.isHidden("#chap-jump-wrap"));
  check("no errors downloading an e-book", E.errors.length === 0, E.errors);
  await E.close();

  console.log("Going to a chapter in a long novel");
  const J = await open(browser);
  const asks = [];
  await J.route(/\/api\/novel\/[^/]+\/chapters\?limit=100&offset=\d+$/, (r) => {
    const offset = +new URL(r.request().url()).searchParams.get("offset");
    asks.push(offset);
    const items = [];
    for (let i = offset; i < Math.min(offset + 100, 11000); i++) {
      items.push({ id: "c" + (i + 1), serial: i + 1, title: "Chapter " + (i + 1), is_available: true });
    }
    r.fulfill({ json: { items: items, total: 11000, offset: offset, limit: 100 } });
  });
  await search(J, "reading now");
  await J.click("#lib-list .libcard h3"); await J.waitForTimeout(600);
  check("a long chapter list offers Go to chapter", await J.isVisible("#chap-jump-wrap"));
  await J.fill("#chap-jump", "9000"); await J.click("#chap-jump-go"); await J.waitForTimeout(500);
  const firstShown = await J.$eval("#chap-list .chap .n", (n) => n.textContent);
  check("Go to chapter lists from there", asks[asks.length - 1] === 8994 && firstShown === "#8995", { asks, firstShown });
  check("and offers the start again", await J.isVisible("#chap-jump-start"));
  await J.click("#chap-jump-start"); await J.waitForTimeout(500);
  check("From the start lists chapter 1 first", (await J.$eval("#chap-list .chap .n", (n) => n.textContent)) === "#1");
  check("no errors going to a chapter", J.errors.length === 0, J.errors);
  await J.close();

  console.log("Delete unfinished novels");
  const U = await open(browser);
  let asked = null, unfPolls = 0, recountsWhileRunning = 0;
  const found = { count: 5, details_only: 3, partial: 2, chapters: 40, kept: 1 };
  await U.route(/\/api\/novels\/unfinished(\?.*)?$/, (r) => {
    unfPolls++;
    const running = asked ? unfPolls < 4 : false;
    const removing = asked ? { running, kind: asked, done: Math.min(unfPolls - 1, 2), total: 2, failed: 0, current: "A Novel" } : { running: false };
    const progressOnly = new URL(r.request().url()).searchParams.get("progress_only") === "true";
    if (!progressOnly && running) recountsWhileRunning++;
    r.fulfill({ json: progressOnly ? { removing } : Object.assign({}, found, { removing }) });
  });
  await U.route(/\/api\/novels\/unfinished\/remove/, (r) => { asked = new URL(r.request().url()).searchParams.get("kind"); unfPolls = 1; r.fulfill({ json: {} }); });
  await U.evaluate(() => document.getElementById("lib-unfinished").click());
  await U.waitForTimeout(300);
  check("it offers details-only, partly downloaded or both, with counts",
    /3 novel/.test(await U.textContent("#unf-details")) && /2 novel.*40 chapter/.test(await U.textContent("#unf-partial")) && /5 novel/.test(await U.textContent("#unf-all")));
  await U.check('input[name="unf-kind"][value="partial"]');
  check("the button counts the chosen kind", /Delete 2 novel/.test(await U.textContent("#unf-go")));
  if (process.env.SHOTS) await U.locator("#lib-unf").screenshot({ path: process.env.SHOTS + "/unfinished-choice.png" });
  U.once("dialog", (d) => d.accept());
  await U.click("#unf-go");
  await U.waitForTimeout(300);
  check("only the chosen kind is asked for", asked === "partial", asked);
  if (process.env.SHOTS) await U.locator("#lib-unf").screenshot({ path: process.env.SHOTS + "/unfinished-progress.png" });
  check("a bar shows how far it has got", await U.isVisible("#unf-progress") && /Deleting/.test(await U.textContent("#unf-text")), await U.textContent("#unf-text"));
  await U.waitForTimeout(3500);
  check("it says when it is done", /Deleted 2 of 2/.test(await U.textContent("#unf-text")), await U.textContent("#unf-text"));
  check("while it runs only the progress is asked for, not a recount", recountsWhileRunning === 0, recountsWhileRunning);
  check("no errors deleting unfinished novels", U.errors.length === 0, U.errors);
  await U.close();

  console.log("Turning pages");
  {
    const R = await open(browser);
    const reads = [], marked = [];
    R.on("request", (r) => {
      if (/\/api\/chapter\/[^/]+\/read/.test(r.url())) reads.push(r.url());
      if (r.method() === "POST" && /\/api\/read-history\/add\//.test(r.url())) marked.push(r.url());
    });
    const chapters = await R.evaluate(async ([t, id]) => (await (await fetch(
      "/api/novel/" + id + "/chapters?limit=10", { headers: { Authorization: "Bearer " + t } })).json()).items, [TOKEN, IDS["Reading Now"]]);
    const second = chapters.find((c) => c.serial === 2).id;
    await search(R, "reading now");
    await R.click("#lib-list .libcard >> nth=0"); await R.waitForSelector(".chap");
    await R.click(".chap >> nth=0");
    await R.waitForFunction(() => document.getElementById("reader-title").textContent === "Chapter 1");
    await R.waitForTimeout(500);
    const ofSecond = () => reads.filter((u) => u.includes(second));
    check("the next chapter is fetched while this one is read, not as read",
      ofSecond().length === 1 && /preload=true/.test(ofSecond()[0]) && marked.length === 0, { reads, marked });
    await R.click("#next-btn");
    await R.waitForFunction(() => document.getElementById("reader-title").textContent === "Chapter 2", null, { timeout: 2000 });
    await R.waitForTimeout(500);
    check("Next shows it without asking for it again", ofSecond().length === 1, ofSecond());
    check("opened, it is counted as read", marked.length === 1 && marked[0].includes(second), marked);
    check("and the one after is fetched ahead in turn",
      reads.some((u) => u.includes(chapters.find((c) => c.serial === 3).id) && /preload=true/.test(u)), reads);
    check("no errors turning pages", R.errors.length === 0, R.errors);
    await R.close();
  }

  console.log("Offline copies");
  {
    // its own context: the others block the service worker (see open())
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const S = await context.newPage();
    S.errors = [];
    S.on("pageerror", (e) => S.errors.push(e.message));
    await S.goto(READER);
    await S.evaluate((t) => localStorage.setItem("lncrawl_token", t), TOKEN);
    await S.evaluate(() => navigator.serviceWorker.ready);
    await S.goto(READER); // now under the service worker's control
    await S.waitForFunction(() => /novel\(s\)/.test(document.getElementById("lib-status").textContent));
    const kept = await S.evaluate(async ([t, novel]) => {
      const get = (u) => fetch(u, { headers: { Authorization: "Bearer " + t } });
      await get("/api/jobs?limit=5");
      await get("/api/novels/unfinished");
      await get("/api/novels?limit=5");
      const chapter = (await (await get("/api/novel/" + novel + "/chapters?limit=1")).json()).items[0].id;
      await get("/api/chapter/" + chapter + "/read?auto_fetch=false&preload=true");
      await new Promise((r) => setTimeout(r, 500));
      const urls = async (name) => (await (await caches.open(name)).keys()).map((r) => new URL(r.url).pathname);
      const auto = await (await caches.open("lncrawl-reader-auto-v1")).keys();
      const reads = auto.map((r) => new URL(r.url)).filter((u) => u.pathname.includes(chapter)).map((u) => u.search);
      return { auto: await urls("lncrawl-reader-auto-v1"), saved: await urls("lncrawl-reader-v1"), reads };
    }, [TOKEN, IDS["Reading Now"]]);
    check("a chapter fetched ahead is kept as itself, to open again offline",
      JSON.stringify(kept.reads) === '["?auto_fetch=false"]', kept.reads);
    check("what is read is kept for offline", kept.auto.includes("/api/novels"), kept.auto);
    check("job and unfinished polls are not kept",
      !kept.auto.concat(kept.saved).some((p) => p.startsWith("/api/job") || p.startsWith("/api/novels/unfinished")), kept);
    check("copies on the way past stay out of the saved-for-offline cache",
      !kept.saved.some((p) => p.startsWith("/api/")), kept.saved);
    check("no errors with the service worker", S.errors.length === 0, S.errors);
    await context.close();
  }

  console.log("Phone");
  await A.setViewportSize({ width: 390, height: 800 }); await A.waitForTimeout(300);
  const overflow = await A.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check("nothing runs off the side on a phone", overflow <= 1, overflow);

  check("no errors in the page", A.errors.length === 0 && B.errors.length === 0, A.errors.concat(B.errors));
  await browser.close();
  console.log(failures ? failures + " check(s) failed" : "all checks passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
