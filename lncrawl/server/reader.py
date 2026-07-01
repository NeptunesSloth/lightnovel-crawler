"""Standalone in-app Reader page.

A self-contained, dependency-free reading experience served at ``GET /reader``.
It browses the downloaded library, lists a novel's chapters, and renders chapter
content (text and manga images) with next/previous navigation, adjustable font
size, and per-novel reading-position memory. Kept outside ``lncrawl/server/web``
(overwritten by the web-sync workflow) so it survives front-end rebuilds; the
markup is embedded as a string so it always ships with the wheel/exe.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

router = APIRouter()

# NOTE: plain string (not an f-string) on purpose — the embedded CSS/JS is full
# of ``{}`` braces. Avoid backslash escapes inside the JS strings.
_READER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta name="theme-color" content="#0f1115" />
<meta name="mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
<meta name="apple-mobile-web-app-title" content="LNCrawl" />
<link rel="manifest" href="/reader/manifest.webmanifest" />
<link rel="apple-touch-icon" href="/reader/icon.svg" />
<title>LNCrawl · Reader</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #0f1115; color: #e6e6e6; line-height: 1.6;
  }
  .topbar {
    position: sticky; top: 0; z-index: 10; display: flex; gap: 10px; align-items: center;
    padding: 10px 16px; background: #141821; border-bottom: 1px solid #262b36;
  }
  .topbar h1 { font-size: 16px; margin: 0; flex: 0 0 auto; color: #cbd2dd; }
  .topbar .spacer { flex: 1; }
  input[type=text] {
    padding: 8px 11px; border-radius: 8px; border: 1px solid #303644;
    background: #0f1115; color: #e6e6e6; font-size: 14px; width: 100%;
  }
  button {
    padding: 8px 13px; border: 0; border-radius: 8px; background: #2a2f3a; color: #cbd2dd;
    font-size: 14px; cursor: pointer;
  }
  button:hover { background: #343a47; }
  button.primary { background: #3b82f6; color: #fff; font-weight: 600; }
  button.primary:hover { background: #2f6fe0; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 18px 16px 80px; }
  .muted { color: #8b93a1; }
  .hidden { display: none !important; }
  .card {
    background: #171a21; border: 1px solid #262b36; border-radius: 10px;
    padding: 12px 14px; margin-bottom: 10px; cursor: pointer;
  }
  .card:hover { border-color: #3b4250; }
  .card h3 { margin: 0 0 3px; font-size: 15px; }
  .card .meta { color: #8b93a1; font-size: 13px; }
  .libcard { display: flex; gap: 12px; align-items: flex-start; }
  .libcard .cover {
    width: 64px; height: 92px; flex: 0 0 64px; border-radius: 6px; object-fit: cover;
    background: #0b0d11; border: 1px solid #262b36;
  }
  .libcard .body { flex: 1; min-width: 0; }
  .libcard .desc {
    color: #aab2c0; font-size: 13px; margin: 5px 0 0;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
  .chip {
    font-size: 11px; color: #aab2c0; background: #20242d; border: 1px solid #2a2f3a;
    border-radius: 999px; padding: 2px 9px; cursor: pointer; white-space: nowrap;
  }
  .chip:hover { color: #e6e6e6; border-color: #3b4250; }
  .chip.active { background: #3b82f6; color: #fff; border-color: #3b82f6; }
  .toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
  select {
    padding: 7px 10px; border-radius: 8px; border: 1px solid #303644;
    background: #0f1115; color: #e6e6e6; font-size: 13px;
  }
  #novel-cover {
    width: 110px; height: 158px; border-radius: 8px; object-fit: cover; float: left;
    margin: 0 16px 8px 0; background: #0b0d11; border: 1px solid #262b36;
  }
  #novel-synopsis { color: #cbd2dd; font-size: 14px; margin: 6px 0; }
  .shelf-title { font-size: 14px; color: #cbd2dd; margin: 2px 0 8px; }
  .shelf { display: flex; gap: 10px; overflow-x: auto; padding-bottom: 8px; margin-bottom: 6px; }
  .shelf .mini { flex: 0 0 104px; cursor: pointer; }
  .shelf .mini img {
    width: 104px; height: 148px; object-fit: cover; border-radius: 6px; display: block;
    background: #0b0d11; border: 1px solid #262b36;
  }
  .shelf .mini .mt {
    font-size: 12px; color: #cbd2dd; margin-top: 5px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .progress { height: 4px; background: #20242d; border-radius: 999px; margin-top: 6px; overflow: hidden; }
  .progress > span { display: block; height: 100%; background: #3b82f6; }
  .badge {
    font-size: 10px; color: #aab2c0; background: #20242d; border: 1px solid #2a2f3a;
    border-radius: 4px; padding: 1px 6px; margin-left: 7px; vertical-align: middle;
  }
  .badge.new { color: #7ee2a8; border-color: #2f5d43; background: #12291c; }
  .chap {
    display: flex; justify-content: space-between; gap: 10px; align-items: center;
    padding: 9px 6px; border-bottom: 1px solid #20242d; cursor: pointer;
  }
  .chap:hover { background: #171a21; }
  .chap .n { color: #6b7280; font-size: 12px; min-width: 48px; }
  .chap .t { flex: 1; font-size: 14px; }
  .chap.unavail .t { color: #6b7280; }
  #reader-content { font-size: 18px; }
  #reader-content img { max-width: 100%; display: block; margin: 6px auto; border-radius: 4px; }
  #reader-content h1 { font-size: 22px; }
  .reader-nav {
    position: sticky; bottom: 0; display: flex; gap: 10px; align-items: center;
    padding: 10px 0; background: linear-gradient(180deg, rgba(15,17,21,0), #0f1115 40%);
  }
  .back { color: #7aa2ff; cursor: pointer; font-size: 14px; text-decoration: none; }
  .back:hover { text-decoration: underline; }
  #auth { max-width: 360px; margin: 60px auto; }
  label { display:block; font-size: 13px; color: #aab2c0; margin: 10px 0 4px; }

  /* ---- Aa settings panel ---- */
  #aa-panel {
    position: fixed; top: 52px; right: 12px; z-index: 30; width: 270px;
    background: #171a21; border: 1px solid #303644; border-radius: 10px;
    padding: 12px 14px; box-shadow: 0 8px 30px rgba(0,0,0,.45);
  }
  .aa-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 8px 0; flex-wrap: wrap; }
  .aa-label { font-size: 13px; color: #aab2c0; }

  /* ---- Reading themes ---- */
  body.t-light { background: #f6f6f1; color: #24262b; }
  body.t-light .topbar { background: #ffffff; border-color: #dcdcd4; }
  body.t-light .topbar h1 { color: #444; }
  body.t-light .card, body.t-light #aa-panel { background: #ffffff; border-color: #dcdcd4; }
  body.t-light .chap { border-color: #e6e6de; }
  body.t-light .chap:hover, body.t-light .card:hover { background: #f0f0e8; }
  body.t-light input[type=text], body.t-light select { background: #fff; color: #24262b; border-color: #ccc; }
  body.t-light button { background: #e8e8e0; color: #333; }
  body.t-light .muted, body.t-light .card .meta { color: #6b7280; }
  body.t-light .chip { background: #eee; color: #555; border-color: #ddd; }
  body.t-light #novel-synopsis { color: #3a3d44; }
  body.t-sepia { background: #f4ecd8; color: #4b3b2a; }
  body.t-sepia .topbar { background: #efe5cd; border-color: #ddd0b5; }
  body.t-sepia .topbar h1 { color: #6a543c; }
  body.t-sepia .card, body.t-sepia #aa-panel { background: #efe5cd; border-color: #ddd0b5; }
  body.t-sepia .chap { border-color: #e4d8bd; }
  body.t-sepia .chap:hover, body.t-sepia .card:hover { background: #eadfc4; }
  body.t-sepia input[type=text], body.t-sepia select { background: #f8f2e2; color: #4b3b2a; border-color: #cbbd9d; }
  body.t-sepia button { background: #e2d5b5; color: #55442f; }
  body.t-sepia .muted, body.t-sepia .card .meta { color: #8a785f; }
  body.t-sepia .chip { background: #e8dcc0; color: #6a583e; border-color: #d5c6a3; }
  body.t-sepia #novel-synopsis { color: #55442f; }
  #reader-content.f-serif { font-family: Georgia, "Times New Roman", serif; }

  /* ---- Manga image fit ---- */
  #reader-content.fit-h img { max-height: 94vh; width: auto; max-width: 100%; }
  #reader-content.strip img { margin: 0 auto; border-radius: 0; }

  /* ---- Bookmarks ---- */
  .bmk-row {
    display: flex; align-items: center; gap: 8px; padding: 7px 6px;
    border-bottom: 1px solid #20242d; font-size: 13px; cursor: pointer;
  }
  .bmk-row:hover { background: #171a21; }
  .bmk-row .x { color: #6b7280; padding: 0 6px; }
  .bmk-row .x:hover { color: #e66; }

  /* ---- Tap-translate popup ---- */
  #trans-pop {
    position: fixed; z-index: 40; max-width: 300px; font-size: 14px;
    background: #1d2129; border: 1px solid #3b4250; border-radius: 8px;
    padding: 8px 11px; box-shadow: 0 6px 24px rgba(0,0,0,.5);
  }
  #trans-pop .w { color: #7aa2ff; font-weight: 600; }

  /* ---- Mobile / small screens ---- */
  @media (max-width: 640px) {
    .topbar { flex-wrap: wrap; gap: 8px; padding: 8px 12px; padding-top: max(8px, env(safe-area-inset-top)); }
    .topbar h1 { font-size: 15px; }
    #search-wrap { order: 3; flex: 1 1 100%; max-width: none; }
    .wrap { padding: 14px 12px 90px; }
    button { padding: 10px 14px; }            /* larger tap targets */
    .back { padding: 6px 2px; }
    #novel-cover { width: 92px; height: 132px; margin: 0 12px 6px 0; }
    .libcard .cover { width: 56px; height: 80px; flex-basis: 56px; }
    #reader-content { font-size: 17px; }
    .reader-nav { padding: 10px 0 max(10px, env(safe-area-inset-bottom)); }
    .reader-nav button { padding: 12px 16px; }
  }
  @media (hover: none) {
    .card:active { border-color: #3b4250; }    /* touch feedback */
  }
</style>
</head>
<body>
<div class="topbar">
  <h1>📖 LNCrawl Reader</h1>
  <a class="back hidden" id="nav-back">&larr; Back</a>
  <div class="spacer"></div>
  <div id="search-wrap" style="flex:1;max-width:380px"><input id="search" type="text" placeholder="Search your library…" /></div>
  <a class="back" href="/tools" target="_blank" id="tools-link">Tools</a>
  <button id="fit-btn" class="hidden" title="Image fit mode">Fit: width</button>
  <button id="aa-btn" class="hidden" title="Reading settings">Aa</button>
</div>

<div id="aa-panel" class="hidden">
  <div class="aa-row"><span class="aa-label">Size</span>
    <button id="font-dn">A-</button><button id="font-up">A+</button>
  </div>
  <div class="aa-row"><span class="aa-label">Theme</span><span class="tags" id="aa-theme"></span></div>
  <div class="aa-row"><span class="aa-label">Font</span><span class="tags" id="aa-font"></span></div>
  <div class="aa-row"><span class="aa-label">Width</span><span class="tags" id="aa-width"></span></div>
  <div class="aa-row"><span class="aa-label">Tap translate <small class="muted">(to English)</small></span><span class="tags" id="aa-trans"></span></div>
</div>
<div id="trans-pop" class="hidden"></div>

<div class="wrap">
  <div id="auth" class="hidden">
    <h2>Sign in</h2>
    <label for="email">Email</label>
    <input id="email" type="text" autocomplete="username" />
    <label for="password">Password</label>
    <input id="password" type="text" autocomplete="current-password" style="-webkit-text-security:disc" />
    <button id="login" class="primary" style="margin-top:14px">Sign in</button>
    <p class="muted" id="auth-msg"></p>
  </div>

  <div id="view-library">
    <div id="lib-continue-wrap" class="hidden">
      <div class="shelf-title">⏵ Continue reading</div>
      <div id="lib-continue" class="shelf"></div>
    </div>
    <div class="toolbar">
      <select id="lib-sort" title="Sort">
        <option value="recent">Recently added</option>
        <option value="updated">Recently updated</option>
        <option value="chapters">Most chapters</option>
        <option value="title">Title A–Z</option>
      </select>
      <button id="lib-update" title="Re-check every novel on its source for new chapters">⟳ Check updates</button>
      <button id="lib-select" title="Select novels to delete">☑ Select</button>
      <button id="lib-del" class="hidden">🗑 Delete (0)</button>
      <div id="lib-cats" class="tags"></div>
    </div>
    <p class="muted" id="lib-status">Loading your library…</p>
    <div id="lib-list"></div>
  </div>

  <div id="view-novel" class="hidden">
    <img id="novel-cover" class="hidden" alt="" />
    <h2 id="novel-title"></h2>
    <p class="meta muted" id="novel-meta"></p>
    <div id="novel-tags" class="tags"></div>
    <p id="novel-synopsis"></p>
    <div style="clear:both"></div>
    <button id="continue-btn" class="primary hidden" style="margin-bottom:12px">▶ Continue reading</button>
    <button id="fav-btn" style="margin:0 0 12px 8px" title="Add to favorites">☆ Favorite</button>
    <button id="heal-btn" class="hidden" style="margin:0 0 12px 8px" title="Fill gaps from another copy of this novel in your library">✨ Fill missing chapters</button>
    <button id="merge-btn" class="hidden" style="margin:0 0 12px 8px" title="Combine all copies of this title into the most complete one">⧉ Merge copies</button>
    <button id="del-btn" style="margin:0 0 12px 8px" title="Delete this novel from your library">🗑</button>
    <span class="muted hidden" id="heal-msg" style="margin-left:8px"></span>
    <div id="bmk-list"></div>
    <div id="chap-list"></div>
    <div id="chap-more-wrap" class="hidden" style="text-align:center;margin-top:12px">
      <button id="chap-more">Load more chapters</button>
    </div>
  </div>

  <div id="view-reader" class="hidden">
    <h2 id="reader-title"></h2>
    <div id="reader-content"></div>
    <div class="reader-nav">
      <button id="prev-btn">&larr; Prev</button>
      <div class="spacer" style="flex:1"></div>
      <button id="bmk-btn" title="Bookmark this spot">🔖</button>
      <span class="muted" id="reader-pos"></span>
      <div class="spacer" style="flex:1"></div>
      <button id="next-btn" class="primary">Next &rarr;</button>
    </div>
  </div>
</div>

<script>
(function () {
  var KEY = "lncrawl_token";
  var POS = "lncrawl_reader_pos";    // { novelId: {id, serial, ts} | legacy chapterId string }
  var FONT = "lncrawl_reader_font";
  var VIEW = "lncrawl_reader_view";  // { sort, cat }
  var REFRESH = "lncrawl_reader_refresh";  // { novelId: last info-refresh ts }
  var HEALED = "lncrawl_reader_autoheal";  // { novelId: last auto-heal attempt ts }
  var UPD = "lncrawl_reader_updates";  // { novelId: {found, ts} } new chapters found
  var PREFS = "lncrawl_reader_prefs";  // { theme, font, width, translate, fit }
  var FAV = "lncrawl_reader_favs";  // [novelId, ...]
  var BMK = "lncrawl_reader_bookmarks";  // { novelId: [{id, serial, scroll, title, ts}] }

  (function () {
    var params = new URLSearchParams(window.location.search);
    var t = params.get("authToken");
    if (t) {
      localStorage.setItem(KEY, t);
      params.delete("authToken");
      var qs = params.toString();
      window.history.replaceState({}, document.title, window.location.pathname + (qs ? "?" + qs : ""));
    }
  })();

  function token() { return localStorage.getItem(KEY) || ""; }
  function api(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers["Authorization"] = "Bearer " + token();
    opts.headers["Content-Type"] = "application/json";
    return fetch("/api" + path, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
        if (!res.ok) {
          var msg = (data && (data.error || data.detail)) || ("HTTP " + res.status);
          throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
        }
        return data;
      });
    });
  }

  function loadPos() { try { return JSON.parse(localStorage.getItem(POS) || "{}"); } catch (e) { return {}; } }
  function posOf(novelId) {
    var v = loadPos()[novelId];
    if (!v) return null;
    if (typeof v === "string") return { id: v, serial: null, ts: 0 };  // legacy
    return v;
  }
  function savePos(novelId, chapterId, serial) {
    var p = loadPos();
    // keep the in-chapter scroll position when re-saving the same chapter;
    // start a newly-opened chapter at the top
    var prev = p[novelId];
    var scroll = (prev && typeof prev === "object" && prev.id === chapterId) ? (prev.scroll || 0) : 0;
    p[novelId] = { id: chapterId, serial: (serial != null ? serial : null), ts: Date.now(), scroll: scroll };
    localStorage.setItem(POS, JSON.stringify(p));
  }
  function saveScroll(novelId, chapterId, frac) {
    var p = loadPos();
    var e = p[novelId];
    if (!e || typeof e === "string" || e.id !== chapterId) return;
    e.scroll = frac; e.ts = Date.now();
    localStorage.setItem(POS, JSON.stringify(p));
  }
  function loadUpdates() { try { return JSON.parse(localStorage.getItem(UPD) || "{}"); } catch (e) { return {}; } }
  function saveUpdates(m) { localStorage.setItem(UPD, JSON.stringify(m)); }
  function loadPrefs() { try { return JSON.parse(localStorage.getItem(PREFS) || "{}"); } catch (e) { return {}; } }
  function savePrefs(p) { localStorage.setItem(PREFS, JSON.stringify(p)); }
  function loadFavs() { try { return JSON.parse(localStorage.getItem(FAV) || "[]"); } catch (e) { return []; } }
  function saveFavs(a) { localStorage.setItem(FAV, JSON.stringify(a)); }
  function loadBmks() { try { return JSON.parse(localStorage.getItem(BMK) || "{}"); } catch (e) { return {}; } }
  function saveBmks(m) { localStorage.setItem(BMK, JSON.stringify(m)); }
  function loadView() { try { return JSON.parse(localStorage.getItem(VIEW) || "{}"); } catch (e) { return {}; } }
  function saveView() {
    localStorage.setItem(VIEW, JSON.stringify({ sort: els["lib-sort"].value, cat: lib.cat }));
  }

  var els = {};
  ["nav-back","search","view-library","lib-continue-wrap","lib-continue","lib-sort","lib-cats",
   "lib-update","lib-status","lib-list","view-novel","novel-cover","novel-title","novel-meta","novel-tags",
   "novel-synopsis","continue-btn","heal-btn","heal-msg","chap-list","chap-more-wrap","chap-more",
   "view-reader","reader-title","reader-content","prev-btn","next-btn","reader-pos","font-up",
   "font-dn","auth","email","password","login","auth-msg","search-wrap","aa-btn","aa-panel",
   "aa-theme","aa-font","aa-width","aa-trans","fit-btn","fav-btn","bmk-btn","bmk-list",
   "trans-pop","lib-select","lib-del","merge-btn","del-btn"].forEach(function (id) {
    els[id] = document.getElementById(id);
  });

  function show(view) {
    ["view-library","view-novel","view-reader","auth"].forEach(function (v) {
      els[v].classList.toggle("hidden", v !== view);
    });
    els["nav-back"].classList.toggle("hidden", view === "view-library" || view === "auth");
    els["search-wrap"].classList.toggle("hidden", view !== "view-library");
    var inReader = view === "view-reader";
    els["aa-btn"].classList.toggle("hidden", !inReader);
    if (!inReader) {
      els["aa-panel"].classList.add("hidden");
      els["fit-btn"].classList.add("hidden");
      hideTransPop();
    }
  }

  // ---- Library ----
  var lib = { all: [], cat: "", manage: false, sel: {} };
  function normTitle(t) { return (t || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim(); }

  // Load a cover through the authenticated endpoint and swap in an object URL.
  function loadCover(img, novelId) {
    fetch("/api/novel/" + novelId + "/cover", { headers: { "Authorization": "Bearer " + token() } })
      .then(function (r) { if (!r.ok) throw new Error("cover"); return r.blob(); })
      .then(function (b) { img.src = URL.createObjectURL(b); img.classList.remove("hidden"); })
      .catch(function () { /* leave placeholder */ });
  }

  function loadLibrary(q) {
    els["lib-status"].textContent = "Loading your library…";
    els["lib-list"].innerHTML = "";
    api("/novels?limit=100&search=" + encodeURIComponent(q || "")).then(function (res) {
      lib.all = (res && res.items) || [];
      renderContinue();
      renderCategories();
      renderLibrary();
    }).catch(function (e) { els["lib-status"].textContent = "Couldn't load library: " + e.message; });
  }

  // A "Continue reading" shelf of the most recently opened novels.
  function renderContinue() {
    var byId = {}; lib.all.forEach(function (n) { byId[n.id] = n; });
    var recent = [];
    var posMap = loadPos();
    Object.keys(posMap).forEach(function (id) {
      var n = byId[id]; var pe = posOf(id);
      if (n && pe) recent.push({ n: n, pe: pe });
    });
    recent.sort(function (a, b) { return (b.pe.ts || 0) - (a.pe.ts || 0); });
    recent = recent.slice(0, 12);
    els["lib-continue"].innerHTML = "";
    els["lib-continue-wrap"].classList.toggle("hidden", !recent.length);
    recent.forEach(function (r) {
      var mini = document.createElement("div"); mini.className = "mini";
      var img = document.createElement("img"); img.alt = "";
      if (r.n.cover_available) loadCover(img, r.n.id);
      var t = document.createElement("div"); t.className = "mt"; t.textContent = r.n.title || "Untitled";
      mini.appendChild(img); mini.appendChild(t);
      if (r.pe.serial && r.n.chapter_count) {
        var pr = document.createElement("div"); pr.className = "progress";
        var sp = document.createElement("span");
        sp.style.width = Math.min(100, Math.round(100 * r.pe.serial / r.n.chapter_count)) + "%";
        pr.appendChild(sp); mini.appendChild(pr);
      }
      mini.addEventListener("click", function () {
        current = { novel: r.n, chapters: [], offset: 0 };
        openChapter(r.pe.id);
      });
      els["lib-continue"].appendChild(mini);
    });
  }

  function renderCategories() {
    var counts = {};
    lib.all.forEach(function (n) {
      (n.tags || []).forEach(function (t) { if (t) counts[t] = (counts[t] || 0) + 1; });
    });
    var top = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; }).slice(0, 12);
    els["lib-cats"].innerHTML = "";
    // favorites filter first, then the top categories
    var chips = [{ v: "__fav", t: "★ Favorites" }].concat(top.map(function (t) { return { v: t, t: t }; }));
    if (chips.length === 1 && !loadFavs().length) return;
    chips.forEach(function (c) {
      var chip = document.createElement("span");
      chip.className = "chip" + (lib.cat === c.v ? " active" : "");
      chip.textContent = c.t;
      chip.addEventListener("click", function () {
        lib.cat = (lib.cat === c.v) ? "" : c.v;
        renderCategories(); renderLibrary();
      });
      els["lib-cats"].appendChild(chip);
    });
  }

  function renderLibrary() {
    saveView();
    var items = lib.all.slice();
    if (lib.cat === "__fav") {
      var favs = loadFavs();
      items = items.filter(function (n) { return favs.indexOf(n.id) >= 0; });
    } else if (lib.cat) {
      items = items.filter(function (n) { return (n.tags || []).indexOf(lib.cat) >= 0; });
    }

    var sort = els["lib-sort"].value;
    items.sort(function (a, b) {
      if (sort === "title") return (a.title || "").localeCompare(b.title || "");
      if (sort === "chapters") return (b.chapter_count || 0) - (a.chapter_count || 0);
      if (sort === "updated") return (b.updated_at || 0) - (a.updated_at || 0);
      return (b.created_at || 0) - (a.created_at || 0);  // recent
    });

    els["lib-list"].innerHTML = "";
    if (!items.length) {
      els["lib-status"].textContent = lib.all.length ? "No matches." : "Your library is empty — download something first (Tools).";
      return;
    }
    els["lib-status"].textContent = items.length + " novel(s)" +
      (lib.cat ? (" · " + (lib.cat === "__fav" ? "★ Favorites" : lib.cat)) : "");
    items.forEach(function (n) {
      var c = document.createElement("div");
      c.className = "card libcard";

      var img = document.createElement("img"); img.className = "cover hidden"; img.alt = "";
      if (n.cover_available) loadCover(img, n.id);

      var body = document.createElement("div"); body.className = "body";
      var h = document.createElement("h3"); h.textContent = n.title || "Untitled";
      if (isFav(n.id)) { var st = document.createElement("span"); st.className = "badge"; st.textContent = "★"; h.appendChild(st); }
      if (n.manga) { var bd = document.createElement("span"); bd.className = "badge"; bd.textContent = "Manga"; h.appendChild(bd); }
      var upd = loadUpdates()[n.id];
      if (upd && upd.found) {
        var nb = document.createElement("span"); nb.className = "badge new";
        nb.textContent = "+" + upd.found + " new"; h.appendChild(nb);
      }
      var m = document.createElement("div"); m.className = "meta";
      m.textContent = [n.authors || "", (n.chapter_count || 0) + " chapters", n.domain || ""]
        .filter(Boolean).join("  ·  ");
      body.appendChild(h); body.appendChild(m);

      var pe = posOf(n.id);
      if (pe) {
        var line = document.createElement("div"); line.className = "meta";
        line.style.color = "#7aa2ff";
        line.textContent = "▶ Resume" + (pe.serial ? (" · ch " + pe.serial + (n.chapter_count ? " / " + n.chapter_count : "")) : "");
        body.appendChild(line);
        if (pe.serial && n.chapter_count) {
          var pr = document.createElement("div"); pr.className = "progress";
          var sp = document.createElement("span");
          sp.style.width = Math.min(100, Math.round(100 * pe.serial / n.chapter_count)) + "%";
          pr.appendChild(sp); body.appendChild(pr);
        }
      }

      if (n.synopsis) {
        var d = document.createElement("p"); d.className = "desc";
        d.textContent = n.synopsis.replace(/<[^>]*>/g, " ").replace(/\\s+/g, " ").trim();
        body.appendChild(d);
      }
      if (n.tags && n.tags.length) {
        var tg = document.createElement("div"); tg.className = "tags";
        n.tags.slice(0, 5).forEach(function (t) {
          var chip = document.createElement("span"); chip.className = "chip"; chip.textContent = t;
          tg.appendChild(chip);
        });
        body.appendChild(tg);
      }

      c.appendChild(img); c.appendChild(body);
      if (lib.manage && lib.sel[n.id]) c.style.outline = "2px solid #3b82f6";
      c.addEventListener("click", function () {
        if (!lib.manage) { openNovel(n); return; }
        if (lib.sel[n.id]) { delete lib.sel[n.id]; c.style.outline = ""; }
        else { lib.sel[n.id] = true; c.style.outline = "2px solid #3b82f6"; }
        updateDelBtn();
      });
      els["lib-list"].appendChild(c);
    });
  }

  // ---- Select mode: bulk delete ----
  function updateDelBtn() {
    var n = Object.keys(lib.sel).length;
    els["lib-del"].textContent = "🗑 Delete (" + n + ")";
    els["lib-del"].classList.toggle("hidden", !lib.manage);
  }
  els["lib-select"].addEventListener("click", function () {
    lib.manage = !lib.manage;
    lib.sel = {};
    this.textContent = lib.manage ? "✕ Cancel" : "☑ Select";
    updateDelBtn();
    renderLibrary();
  });
  els["lib-del"].addEventListener("click", function () {
    var ids = Object.keys(lib.sel);
    if (!ids.length) return;
    if (!confirm("Delete " + ids.length + " novel(s) and their downloaded chapters? This cannot be undone.")) return;
    var btn = this;
    btn.disabled = true;
    var i = 0;
    (function step() {
      if (i >= ids.length) {
        btn.disabled = false;
        lib.manage = false; lib.sel = {};
        els["lib-select"].textContent = "☑ Select";
        updateDelBtn();
        loadLibrary(els["search"].value.trim());
        return;
      }
      var id = ids[i++];
      api("/novel/" + id, { method: "DELETE" }).then(step).catch(step);
    })();
  });

  els["lib-sort"].addEventListener("change", renderLibrary);

  // "Check updates": re-fetch every novel's info from its source, one at a time
  // (sequential on purpose — polite to the sources), and badge the ones that
  // gained chapters. Click again to stop mid-run.
  var checking = false;
  els["lib-update"].addEventListener("click", function () {
    var btn = this;
    if (checking) { checking = false; return; }
    if (!lib.all.length) return;
    checking = true;
    btn.textContent = "■ Stop checking";
    var items = lib.all.slice();
    var updates = loadUpdates();
    var i = 0, found = 0;
    function finish() {
      checking = false;
      btn.textContent = "⟳ Check updates";
      renderLibrary();
      els["lib-status"].textContent = found
        ? ("Update check done — " + found + " novel(s) have new chapters.")
        : "Update check done — no new chapters found.";
    }
    function next() {
      if (!checking || i >= items.length) { finish(); return; }
      var n = items[i++];
      els["lib-status"].textContent = "Checking " + i + "/" + items.length + ": " + (n.title || "");
      api("/novel/" + n.id + "/refresh", { method: "POST" }).then(function (r) {
        if (r && (r.chapter_count || 0) > (n.chapter_count || 0)) {
          found++;
          updates[n.id] = { found: (r.chapter_count || 0) - (n.chapter_count || 0), ts: Date.now() };
          saveUpdates(updates);
        }
        if (r) {
          for (var k = 0; k < lib.all.length; k++) {
            if (lib.all[k].id === r.id) lib.all[k] = r;
          }
        }
      }).catch(function () { /* skip novels that fail to refresh */ }).then(next);
    }
    next();
  });

  // ---- Novel ----
  var current = { novel: null, chapters: [], offset: 0 };
  function renderNovelHeader(novel) {
    els["novel-title"].textContent = novel.title || "Untitled";
    els["novel-meta"].textContent = [novel.authors || "", (novel.chapter_count || 0) + " chapters", novel.domain || ""]
      .filter(Boolean).join("  ·  ");

    var cover = els["novel-cover"];
    cover.classList.add("hidden"); cover.removeAttribute("src");
    if (novel.cover_available) loadCover(cover, novel.id);

    els["novel-tags"].innerHTML = "";
    (novel.tags || []).slice(0, 12).forEach(function (t) {
      var chip = document.createElement("span"); chip.className = "chip"; chip.textContent = t;
      els["novel-tags"].appendChild(chip);
    });

    var syn = (novel.synopsis || "").replace(/<[^>]*>/g, " ").replace(/\\s+/g, " ").trim();
    els["novel-synopsis"].textContent = syn;
    els["novel-synopsis"].classList.toggle("hidden", !syn);
  }

  // Backfill: novels downloaded before synopsis/tags parsing have no description.
  // When such a novel is opened, quietly re-fetch its info from the source (at
  // most once a day per novel) and update the view in place — this also picks up
  // newly released chapters.
  function maybeRefreshInfo(novel) {
    if (novel.synopsis) return;
    var map = {};
    try { map = JSON.parse(localStorage.getItem(REFRESH) || "{}"); } catch (e) { map = {}; }
    if (Date.now() - (map[novel.id] || 0) < 86400000) return;
    map[novel.id] = Date.now();
    localStorage.setItem(REFRESH, JSON.stringify(map));
    api("/novel/" + novel.id + "/refresh", { method: "POST" }).then(function (n) {
      if (!n || !current.novel || current.novel.id !== n.id) return;
      var moreChapters = (n.chapter_count || 0) !== (novel.chapter_count || 0);
      current.novel = n;
      renderNovelHeader(n);
      if (moreChapters) {
        current.chapters = []; current.offset = 0; els["chap-list"].innerHTML = "";
        loadChapters();
      }
    }).catch(function () { /* refresh is best-effort */ });
  }

  function openNovel(novel) {
    current = { novel: novel, chapters: [], offset: 0, chapterId: null };
    renderNovelHeader(novel);
    maybeRefreshInfo(novel);

    // opening the novel acknowledges its "+N new" badge
    var updates = loadUpdates();
    if (updates[novel.id]) { delete updates[novel.id]; saveUpdates(updates); }

    renderFavBtn(novel);
    renderBookmarks(novel);

    // offer merging only when another copy of the same title is in the library
    var copies = lib.all.filter(function (n) { return normTitle(n.title) === normTitle(novel.title); });
    els["merge-btn"].classList.toggle("hidden", copies.length < 2);
    if (copies.length > 1) els["merge-btn"].textContent = "⧉ Merge " + copies.length + " copies";

    els["chap-list"].innerHTML = "";
    var pe = posOf(novel.id);
    els["continue-btn"].classList.toggle("hidden", !pe);
    els["continue-btn"].textContent = pe && pe.serial ? ("▶ Continue · ch " + pe.serial) : "▶ Continue reading";
    els["continue-btn"].onclick = function () { if (pe) openChapter(pe.id); };
    els["heal-btn"].classList.add("hidden");
    els["heal-msg"].classList.add("hidden");
    show("view-novel");
    loadChapters();
  }
  function loadChapters() {
    api("/novel/" + current.novel.id + "/chapters?limit=100&offset=" + current.offset).then(function (res) {
      var items = (res && res.items) || [];
      current.chapters = current.chapters.concat(items);
      var gaps = false;
      items.forEach(function (ch) {
        var row = document.createElement("div");
        row.className = "chap" + (ch.is_available ? "" : " unavail");
        var n = document.createElement("span"); n.className = "n"; n.textContent = "#" + ch.serial;
        var t = document.createElement("span"); t.className = "t"; t.textContent = ch.title || ("Chapter " + ch.serial);
        row.appendChild(n); row.appendChild(t);
        if (ch.is_available) row.addEventListener("click", function () { openChapter(ch.id); });
        else { gaps = true; var u = document.createElement("span"); u.className = "muted"; u.textContent = "not downloaded"; row.appendChild(u); }
        els["chap-list"].appendChild(row);
      });
      current.offset += items.length;
      var total = (res && res.total) || current.offset;
      els["chap-more-wrap"].classList.toggle("hidden", current.offset >= total);
      // offer cross-source healing only when there are gaps to fill
      if (gaps) { els["heal-btn"].classList.remove("hidden"); maybeAutoHeal(); }
    }).catch(function (e) { els["chap-list"].innerHTML = "<p class='muted'>Couldn't load chapters: " + e.message + "</p>"; });
  }
  // Auto-heal: when a novel with gaps is opened and another copy of the same
  // title exists in the library, quietly fill what can be filled (at most one
  // attempt per novel per day). The button stays as a manual retry.
  function maybeAutoHeal() {
    var novel = current.novel;
    if (!novel) return;
    var map = {};
    try { map = JSON.parse(localStorage.getItem(HEALED) || "{}"); } catch (e) { map = {}; }
    if (Date.now() - (map[novel.id] || 0) < 86400000) return;
    map[novel.id] = Date.now();
    localStorage.setItem(HEALED, JSON.stringify(map));
    api("/novel/" + novel.id + "/heal", { method: "POST" }).then(function (res) {
      if (!res || !res.healed || !current.novel || current.novel.id !== novel.id) return;
      els["heal-msg"].textContent = "✨ " + (res.message || "Filled missing chapters.");
      els["heal-msg"].classList.remove("hidden");
      current.chapters = []; current.offset = 0; els["chap-list"].innerHTML = "";
      loadChapters();
    }).catch(function () { /* auto-heal is best-effort */ });
  }

  els["heal-btn"].addEventListener("click", function () {
    if (!current.novel) return;
    var btn = this;
    btn.disabled = true;
    els["heal-msg"].textContent = "Looking for another copy in your library…";
    els["heal-msg"].classList.remove("hidden");
    api("/novel/" + current.novel.id + "/heal", { method: "POST" }).then(function (res) {
      els["heal-msg"].textContent = (res && res.message) || "Done.";
      if (res && res.healed) {
        // reload the chapter list to reflect the filled chapters
        current.chapters = []; current.offset = 0; els["chap-list"].innerHTML = "";
        loadChapters();
      }
    }).catch(function (e) {
      els["heal-msg"].textContent = "Heal failed: " + e.message;
    }).finally(function () { btn.disabled = false; });
  });

  els["merge-btn"].addEventListener("click", function () {
    if (!current.novel) return;
    if (!confirm('Combine all copies of this title into the most complete one and delete the others? Chapters that only exist in a deleted copy under a different chapter name can be lost.')) return;
    var btn = this; btn.disabled = true;
    api("/novel/" + current.novel.id + "/merge", { method: "POST" }).then(function (res) {
      var cid = res && res.canonical_id;
      return api("/novels?limit=100&search=").then(function (r) {
        lib.all = (r && r.items) || [];
        renderCategories();
        var canon = null;
        for (var i = 0; i < lib.all.length; i++) { if (lib.all[i].id === cid) canon = lib.all[i]; }
        if (canon) openNovel(canon); else { show("view-library"); renderLibrary(); }
      });
    }).catch(function (e) { alert("Merge failed: " + e.message); })
      .finally(function () { btn.disabled = false; });
  });

  els["del-btn"].addEventListener("click", function () {
    if (!current.novel) return;
    if (!confirm('Delete "' + (current.novel.title || "this novel") + '" and all its downloaded chapters? This cannot be undone.')) return;
    api("/novel/" + current.novel.id, { method: "DELETE" }).then(function () {
      show("view-library");
      loadLibrary(els["search"].value.trim());
    }).catch(function (e) { alert("Delete failed: " + e.message); });
  });

  // ---- Reader ----
  function openChapter(chapterId, scrollTo) {
    show("view-reader");
    els["reader-title"].textContent = "Loading…";
    els["reader-content"].innerHTML = "";
    els["reader-pos"].textContent = "";
    hideTransPop();
    window.scrollTo(0, 0);
    api("/chapter/" + chapterId + "/read?auto_fetch=false").then(function (res) {
      var ch = res.chapter || {};
      var novel = res.novel || current.novel || {};
      current.novel = novel;
      els["reader-title"].textContent = ch.title || ("Chapter " + ch.serial);
      els["reader-content"].innerHTML = res.content || "<p class='muted'>No content available for this chapter.</p>";
      els["reader-pos"].textContent = "#" + (ch.serial || "");
      var imgCount = hydrateImages(els["reader-content"]);
      els["fit-btn"].classList.toggle("hidden", imgCount < 2);
      applyFit();
      current.chapterId = chapterId;
      savePos(novel.id, chapterId, ch.serial);
      // resume mid-chapter where reading stopped (savePos keeps the saved
      // fraction when re-opening the same chapter, resets it on a new one);
      // an explicit scrollTo (from a bookmark) wins
      var pe = posOf(novel.id);
      var frac = (typeof scrollTo === "number" && scrollTo > 0) ? scrollTo
        : ((pe && pe.id === chapterId && pe.scroll > 0.01) ? pe.scroll : 0);
      if (frac > 0) {
        setTimeout(function () {
          var max = document.documentElement.scrollHeight - window.innerHeight;
          if (max > 0) window.scrollTo(0, frac * max);
        }, 60);
      }
      // warm the next chapter's images so page-turns are instant (manga)
      if (res.next_id && imgCount >= 2) preloadNext(res.next_id);
      els["prev-btn"].disabled = !res.previous_id;
      els["next-btn"].disabled = !res.next_id;
      els["prev-btn"].onclick = function () { if (res.previous_id) openChapter(res.previous_id); };
      els["next-btn"].onclick = function () { if (res.next_id) openChapter(res.next_id); };
    }).catch(function (e) {
      els["reader-title"].textContent = "Error";
      els["reader-content"].innerHTML = "<p class='muted'>Couldn't load chapter: " + e.message + "</p>";
    });
  }

  // Manga images are stored as <img src="images/<id>.jpg">; fetch each through
  // the authenticated image endpoint (via the shared blob cache, so preloaded
  // chapters render instantly) and swap in an object URL. Returns image count.
  function hydrateImages(root) {
    var count = 0;
    var imgs = root.querySelectorAll("img[src]");
    imgs.forEach(function (img) {
      var src = img.getAttribute("src") || "";
      var m = src.match(/images\\/([0-9a-fA-F]+)\\.jpg/);
      if (!m) return;
      count += 1;
      img.removeAttribute("src");
      cacheImage(m[1])
        .then(function (u) { img.src = u; })
        .catch(function () { img.alt = "(image unavailable)"; });
    });
    return count;
  }

  // ---- Font size ----
  function applyFont() {
    var sz = parseInt(localStorage.getItem(FONT) || "18", 10);
    els["reader-content"].style.fontSize = sz + "px";
  }
  els["font-up"].addEventListener("click", function () {
    var sz = Math.min(30, parseInt(localStorage.getItem(FONT) || "18", 10) + 2);
    localStorage.setItem(FONT, sz); applyFont();
  });
  els["font-dn"].addEventListener("click", function () {
    var sz = Math.max(12, parseInt(localStorage.getItem(FONT) || "18", 10) - 2);
    localStorage.setItem(FONT, sz); applyFont();
  });

  // ---- Reading preferences (Aa panel) ----
  var prefs = loadPrefs();
  function applyPrefs() {
    document.body.classList.toggle("t-light", prefs.theme === "light");
    document.body.classList.toggle("t-sepia", prefs.theme === "sepia");
    els["reader-content"].classList.toggle("f-serif", prefs.font === "serif");
    var wrap = document.querySelector(".wrap");
    if (wrap) wrap.style.maxWidth = prefs.width === "narrow" ? "620px" : (prefs.width === "wide" ? "1100px" : "860px");
    applyFit();
  }
  function chipRow(el, options, key, def) {
    el.innerHTML = "";
    var val = prefs[key] || def;
    options.forEach(function (opt) {
      var chip = document.createElement("span");
      chip.className = "chip" + (val === opt.v ? " active" : "");
      chip.textContent = opt.t;
      chip.addEventListener("click", function () {
        prefs[key] = opt.v; savePrefs(prefs); renderAaPanel(); applyPrefs();
      });
      el.appendChild(chip);
    });
  }
  function renderAaPanel() {
    chipRow(els["aa-theme"], [{v:"dark",t:"Dark"},{v:"light",t:"Light"},{v:"sepia",t:"Sepia"}], "theme", "dark");
    chipRow(els["aa-font"], [{v:"system",t:"System"},{v:"serif",t:"Serif"}], "font", "system");
    chipRow(els["aa-width"], [{v:"narrow",t:"Narrow"},{v:"normal",t:"Normal"},{v:"wide",t:"Wide"}], "width", "normal");
    chipRow(els["aa-trans"], [{v:"",t:"Off"},{v:"on",t:"On"}], "translate", "");
  }
  els["aa-btn"].addEventListener("click", function (e) {
    e.stopPropagation();
    renderAaPanel();
    els["aa-panel"].classList.toggle("hidden");
  });
  document.addEventListener("click", function (e) {
    if (!els["aa-panel"].classList.contains("hidden") &&
        !els["aa-panel"].contains(e.target) && e.target !== els["aa-btn"]) {
      els["aa-panel"].classList.add("hidden");
    }
  });

  // ---- Manga: image fit modes + next-chapter preload ----
  function applyFit() {
    var fit = prefs.fit || "width";
    els["reader-content"].classList.toggle("fit-h", fit === "height");
    els["reader-content"].classList.toggle("strip", fit === "strip");
    els["fit-btn"].textContent = "Fit: " + fit;
  }
  els["fit-btn"].addEventListener("click", function () {
    var order = ["width", "height", "strip"];
    var cur = order.indexOf(prefs.fit || "width");
    prefs.fit = order[(cur + 1) % order.length];
    savePrefs(prefs); applyFit();
  });

  // Blob-URL cache shared by the visible chapter and the preloaded next one.
  var imgCache = {}, imgCacheOrder = [];
  function cacheImage(id) {
    if (imgCache[id]) return Promise.resolve(imgCache[id]);
    return fetch("/api/chapter/image/" + id, { headers: { "Authorization": "Bearer " + token() } })
      .then(function (r) { if (!r.ok) throw new Error("img"); return r.blob(); })
      .then(function (b) {
        var u = URL.createObjectURL(b);
        imgCache[id] = u; imgCacheOrder.push(id);
        while (imgCacheOrder.length > 80) {
          var old = imgCacheOrder.shift();
          URL.revokeObjectURL(imgCache[old]); delete imgCache[old];
        }
        return u;
      });
  }
  function imageIdsIn(html) {
    var ids = [], re = new RegExp("images/([0-9a-fA-F]+)[.]jpg", "g"), m;
    while ((m = re.exec(html || ""))) ids.push(m[1]);
    return ids;
  }
  // fetch the next chapter's images into the cache so page-turns are instant
  function preloadNext(chapterId) {
    api("/chapter/" + chapterId + "/read?auto_fetch=false").then(function (res) {
      var ids = imageIdsIn(res && res.content);
      var i = 0;
      (function step() {
        if (i >= ids.length) return;
        cacheImage(ids[i++]).then(step).catch(step);
      })();
    }).catch(function () {});
  }

  // ---- Favorites ----
  function isFav(id) { return loadFavs().indexOf(id) >= 0; }
  function renderFavBtn(novel) {
    var on = isFav(novel.id);
    els["fav-btn"].textContent = on ? "★ Favorited" : "☆ Favorite";
    els["fav-btn"].title = on ? "Remove from favorites" : "Add to favorites";
  }
  els["fav-btn"].addEventListener("click", function () {
    if (!current.novel) return;
    var favs = loadFavs();
    var i = favs.indexOf(current.novel.id);
    if (i >= 0) favs.splice(i, 1); else favs.push(current.novel.id);
    saveFavs(favs);
    renderFavBtn(current.novel);
  });

  // ---- Bookmarks ----
  function renderBookmarks(novel) {
    var list = (loadBmks()[novel.id] || []);
    els["bmk-list"].innerHTML = "";
    if (!list.length) return;
    list.forEach(function (b, idx) {
      var row = document.createElement("div"); row.className = "bmk-row";
      var icon = document.createElement("span"); icon.textContent = "🔖";
      var t = document.createElement("span"); t.className = "t";
      t.textContent = (b.title || ("Chapter " + b.serial)) + " · " + Math.round((b.scroll || 0) * 100) + "%";
      var x = document.createElement("span"); x.className = "x"; x.textContent = "✕"; x.title = "Remove bookmark";
      x.addEventListener("click", function (e) {
        e.stopPropagation();
        var m = loadBmks(); (m[novel.id] || []).splice(idx, 1); saveBmks(m);
        renderBookmarks(novel);
      });
      row.appendChild(icon); row.appendChild(t); row.appendChild(x);
      row.addEventListener("click", function () {
        openChapter(b.id, b.scroll || 0);
      });
      els["bmk-list"].appendChild(row);
    });
  }
  els["bmk-btn"].addEventListener("click", function () {
    if (!current.novel || !current.chapterId) return;
    var max = document.documentElement.scrollHeight - window.innerHeight;
    var frac = max > 0 ? Math.min(1, Math.max(0, window.scrollY / max)) : 0;
    var m = loadBmks();
    var list = m[current.novel.id] || (m[current.novel.id] = []);
    list.push({
      id: current.chapterId,
      serial: null,
      title: els["reader-title"].textContent || "",
      scroll: frac,
      ts: Date.now()
    });
    saveBmks(m);
    var btn = this;
    btn.textContent = "✓";
    setTimeout(function () { btn.textContent = "🔖"; }, 900);
  });

  // ---- Tap-translate (for language learning; enable in the Aa menu) ----
  var transCache = {};
  function hideTransPop() { els["trans-pop"].classList.add("hidden"); }
  function wordAt(e) {
    var sel = window.getSelection && window.getSelection();
    if (sel && !sel.isCollapsed) {
      var chosen = sel.toString().trim();
      if (chosen && chosen.length <= 300) return chosen;
    }
    var node = null, offset = 0;
    if (document.caretRangeFromPoint) {
      var r = document.caretRangeFromPoint(e.clientX, e.clientY);
      if (r) { node = r.startContainer; offset = r.startOffset; }
    } else if (document.caretPositionFromPoint) {
      var p = document.caretPositionFromPoint(e.clientX, e.clientY);
      if (p) { node = p.offsetNode; offset = p.offset; }
    }
    if (!node || node.nodeType !== 3) return "";
    var text = node.textContent || "";
    var isw = function (ch) { return ch && /[A-Za-zÀ-ÖØ-öø-ÿŒœÆæĀ-ž'’-]/.test(ch); };
    if (!isw(text[offset]) && offset > 0) offset -= 1;
    if (!isw(text[offset])) return "";
    var a = offset, b = offset;
    while (a > 0 && isw(text[a - 1])) a -= 1;
    while (b < text.length && isw(text[b])) b += 1;
    return text.slice(a, b).trim();
  }
  function showTransPop(word, x, y) {
    var pop = els["trans-pop"];
    pop.innerHTML = "";
    var w = document.createElement("div"); w.className = "w"; w.textContent = word;
    var t = document.createElement("div"); t.textContent = "…";
    pop.appendChild(w); pop.appendChild(t);
    pop.classList.remove("hidden");
    pop.style.left = Math.min(x, window.innerWidth - 320) + "px";
    pop.style.top = Math.min(y + 18, window.innerHeight - 90) + "px";
    var key = word.toLowerCase();
    var done = function (txt) { t.textContent = txt; };
    if (transCache[key]) { done(transCache[key]); return; }
    api("/translate", { method: "POST", body: JSON.stringify({ text: word, target: "en" }) })
      .then(function (res) {
        var txt = (res && res.translation) || "(no translation)";
        transCache[key] = txt; done(txt);
      })
      .catch(function (err) { done("Translation failed: " + err.message); });
  }
  els["reader-content"].addEventListener("click", function (e) {
    if (prefs.translate !== "on") return;
    if (e.target && e.target.tagName === "IMG") return;
    var word = wordAt(e);
    if (!word) { hideTransPop(); return; }
    e.preventDefault();
    showTransPop(word, e.clientX, e.clientY);
  });
  document.addEventListener("scroll", hideTransPop, { passive: true });
  document.addEventListener("click", function (e) {
    if (!els["trans-pop"].classList.contains("hidden") &&
        !els["trans-pop"].contains(e.target) && !els["reader-content"].contains(e.target)) {
      hideTransPop();
    }
  });

  // ---- Navigation / search ----
  els["nav-back"].addEventListener("click", function () {
    if (!els["view-reader"].classList.contains("hidden")) {
      if (current.novel) openNovel(current.novel); else show("view-library");
    } else { show("view-library"); loadLibrary(els["search"].value.trim()); }
  });
  els["chap-more"].addEventListener("click", loadChapters);
  var searchTimer = null;
  els["search"].addEventListener("input", function () {
    clearTimeout(searchTimer);
    var q = this.value.trim();
    searchTimer = setTimeout(function () { loadLibrary(q); }, 300);
  });
  document.addEventListener("keydown", function (e) {
    if (els["view-reader"].classList.contains("hidden")) return;
    if (e.key === "ArrowRight" && !els["next-btn"].disabled) els["next-btn"].click();
    if (e.key === "ArrowLeft" && !els["prev-btn"].disabled) els["prev-btn"].click();
  });

  // remember the in-chapter position while reading (throttled)
  var scrollTimer = null;
  window.addEventListener("scroll", function () {
    if (els["view-reader"].classList.contains("hidden")) return;
    if (!current.novel || !current.chapterId) return;
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(function () {
      var max = document.documentElement.scrollHeight - window.innerHeight;
      var frac = max > 0 ? Math.min(1, Math.max(0, window.scrollY / max)) : 0;
      saveScroll(current.novel.id, current.chapterId, frac);
    }, 400);
  });

  // ---- Auth ----
  els["login"].addEventListener("click", function () {
    var email = els["email"].value.trim(), password = els["password"].value;
    if (!email || !password) { els["auth-msg"].textContent = "Enter email and password."; return; }
    api("/auth/login", { method: "POST", body: JSON.stringify({ email: email, password: password }) })
      .then(function (res) { localStorage.setItem(KEY, res.token); start(); })
      .catch(function (e) { els["auth-msg"].textContent = "Sign in failed: " + e.message; });
  });

  // Offline support: a service worker caches the app shell and every chapter,
  // cover and image you open, so previously-read content works with no network.
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/reader/sw.js", { scope: "/reader" }).catch(function () { /* offline cache unavailable */ });
  }

  var toolsLink = els["tools-link"];
  function start() {
    if (toolsLink) toolsLink.href = "/tools" + (token() ? ("?authToken=" + encodeURIComponent(token())) : "");
    applyFont();
    applyPrefs();
    var v = loadView();
    if (v.sort) els["lib-sort"].value = v.sort;
    if (v.cat) lib.cat = v.cat;
    if (!token()) { show("auth"); return; }
    api("/auth/me", { method: "GET" })
      .then(function () { show("view-library"); loadLibrary(""); })
      .catch(function () { localStorage.removeItem(KEY); show("auth"); });
  }
  start();
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Offline (PWA) assets: a service worker + manifest + icon make the Reader
# installable on mobile and let it serve previously-opened content with no
# network. The service worker is network-first with a cache fallback, so it is
# always fresh online and still readable offline.
# --------------------------------------------------------------------------- #

_SERVICE_WORKER = """
const CACHE = "lncrawl-reader-v1";
const SHELL = ["/reader", "/reader/manifest.webmanifest", "/reader/icon.svg"];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).catch(function () {}));
  self.skipWaiting();
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

// Network-first, fall back to cache: fresh when online, readable when offline.
self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  e.respondWith(
    fetch(req).then(function (res) {
      if (res && res.ok) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy); }).catch(function () {});
      }
      return res;
    }).catch(function () {
      return caches.match(req).then(function (hit) {
        return hit || (req.mode === "navigate" ? caches.match("/reader") : undefined);
      });
    })
  );
});
"""

_MANIFEST = """{
  "name": "LNCrawl Reader",
  "short_name": "LNCrawl",
  "description": "Read your downloaded light-novel and manga library, online or offline.",
  "start_url": "/reader",
  "scope": "/reader",
  "display": "standalone",
  "orientation": "portrait",
  "background_color": "#0f1115",
  "theme_color": "#0f1115",
  "icons": [
    { "src": "/reader/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable" }
  ]
}"""

_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
  <rect width="192" height="192" rx="36" fill="#0f1115"/>
  <rect x="48" y="40" width="96" height="112" rx="8" fill="#3b82f6"/>
  <rect x="64" y="40" width="16" height="112" fill="#2f6fe0"/>
  <rect x="92" y="64" width="40" height="8" rx="4" fill="#cbd2dd"/>
  <rect x="92" y="84" width="40" height="8" rx="4" fill="#cbd2dd"/>
  <rect x="92" y="104" width="28" height="8" rx="4" fill="#cbd2dd"/>
</svg>"""


@router.get("/reader", include_in_schema=False)
async def reader_page() -> HTMLResponse:
    return HTMLResponse(content=_READER_HTML)


@router.get("/reader/sw.js", include_in_schema=False)
async def reader_service_worker() -> Response:
    # Service-Worker-Allowed lets the worker claim the wider "/reader" scope so
    # it controls the reader page (served at "/reader", not "/reader/").
    return Response(
        content=_SERVICE_WORKER,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/reader", "Cache-Control": "no-cache"},
    )


@router.get("/reader/manifest.webmanifest", include_in_schema=False)
async def reader_manifest() -> Response:
    return Response(content=_MANIFEST, media_type="application/manifest+json")


@router.get("/reader/icon.svg", include_in_schema=False)
async def reader_icon() -> Response:
    return Response(content=_ICON_SVG, media_type="image/svg+xml")
