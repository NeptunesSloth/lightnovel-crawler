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
  .libcard .desc.open { display: block; -webkit-line-clamp: unset; }
  .libcard .desc-more { color: #7aa2ff; font-size: 12px; cursor: pointer; }
  .tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
  .chip {
    font-size: 11px; color: #aab2c0; background: #20242d; border: 1px solid #2a2f3a;
    border-radius: 999px; padding: 2px 9px; cursor: pointer; white-space: nowrap;
  }
  .chip:hover { color: #e6e6e6; border-color: #3b4250; }
  .chip.active { background: #3b82f6; color: #fff; border-color: #3b82f6; }
  .toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
  .toolbar.filters input[type=text] { flex: 1 1 220px; max-width: 360px; margin: 0; }
  .chap-range { display: inline-flex; align-items: center; gap: 5px; font-size: 13px; }
  .chap-range input[type=number] { width: 68px; margin: 0; padding: 6px 8px; }
  .toolbar .check { display: flex; align-items: center; gap: 5px; font-size: 13px; margin: 0; cursor: pointer; }
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
  #lib-web-btn { margin: 0 0 10px; }
  .web-row { display: flex; align-items: center; gap: 8px; padding: 7px 4px; border-bottom: 1px solid #20242d; font-size: 13px; }
  .web-row .t { flex: 1; min-width: 0; }
  .web-row .t b { color: #e6e6e6; }
  .web-row .t .muted { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .web-row button { padding: 4px 10px; font-size: 12px; margin: 0; }
  .novel-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 12px; clear: both; }
  .novel-tools button { margin: 0; }
  .rate { font-size: 20px; letter-spacing: 2px; cursor: pointer; color: #4b5563; user-select: none; }
  .rate .on { color: #f5b301; }
  #shelf-box { margin: 0 0 12px; }
  .badge.fresh { background: #1e3a8a; color: #dbeafe; border-color: #1e40af; }
  .lib-more { height: 1px; }
  .shelf-title .see-all { float: right; font-size: 12px; color: #7aa2ff; cursor: pointer; }
  .shelf .mini .mm { font-size: 11px; color: #8b93a1; margin-top: 2px; }
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
  .bmk-row .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bmk-row .nv { color: #cbd2dd; font-weight: 600; }
  .bmk-row .when { color: #6b7280; font-size: 12px; white-space: nowrap; }
  .libcard .fav-star { cursor: pointer; color: #6b7280; font-size: 18px; line-height: 1; float: right; padding: 0 2px; }
  .libcard .fav-star.on { color: #f5b301; }

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
  <div id="search-wrap" style="flex:1;max-width:380px"><input id="search" type="text" placeholder="Search title, author, description, tags…" /></div>
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
      <div class="shelf-title">⏵ Continue reading
        <a id="lib-continue-all" class="see-all" title="Show everything you are reading, last read first"></a></div>
      <div id="lib-continue" class="shelf"></div>
    </div>
    <div class="toolbar">
      <select id="lib-sort" title="Sort">
        <option value="recent">Recently added</option>
        <option value="read">Recently read</option>
        <option value="updated">Recently updated</option>
        <option value="oldest">Oldest added</option>
        <option value="title">Title A–Z</option>
        <option value="title-desc">Title Z–A</option>
        <option value="chapters">Most chapters</option>
        <option value="chapters-asc">Fewest chapters</option>
        <option value="author">Author A–Z</option>
        <option value="source">Source A–Z</option>
        <option value="newch">New chapters first</option>
        <option value="rating">My rating</option>
        <option value="status">Status: ongoing first</option>
        <option value="status-done">Status: completed first</option>
      </select>
      <select id="lib-kind" title="Media type">
        <option value="">All media</option>
        <option value="novel">Novels</option>
        <option value="comic">All comics</option>
        <option value="manga">Manga (Japanese)</option>
        <option value="manhwa">Manhwa (Korean)</option>
        <option value="manhua">Manhua (Chinese)</option>
      </select>
      <button id="lib-update" title="Re-check every novel on its source for new chapters">⟳ Check updates</button>
      <button id="lib-fill" class="hidden" title="Re-read the details (status, genres, description) of novels that are missing them">✚ Fill in details</button>
      <button id="lib-bmks-btn" title="Every bookmark, across all novels">🔖 Bookmarks</button>
      <button id="lib-auto-btn" title="New chapters: automatic checks and notifications">🔔 New chapters</button>
      <button id="lib-select" title="Select novels to delete">☑ Select</button>
      <button id="lib-del" class="hidden">🗑 Delete (0)</button>
      <div id="lib-cats" class="tags"></div>
    </div>
    <div class="toolbar filters">
      <input id="lib-tag" type="text" list="lib-tag-list" autocomplete="off"
             placeholder="Tags: romance, fantasy, -harem" title="Show novels with every tag listed; put - before a tag to hide it" />
      <datalist id="lib-tag-list"></datalist>
      <select id="lib-source" title="Source"><option value="">All sources</option></select>
      <select id="lib-lang" title="Language"><option value="">All languages</option></select>
      <select id="lib-progress" title="Reading progress">
        <option value="">Any progress</option>
        <option value="unread">Unread</option>
        <option value="reading">Reading</option>
        <option value="finished">Finished</option>
      </select>
      <select id="lib-shelf" title="Shelf"><option value="">All shelves</option></select>
      <select id="lib-state" title="Status">
        <option value="">Any status</option>
        <option value="completed">Completed</option>
        <option value="ongoing">Ongoing</option>
        <option value="hiatus">On hiatus</option>
        <option value="cancelled">Cancelled</option>
        <option value="unknown">Status unknown</option>
      </select>
      <label class="check" title="Show only novels you starred"><input id="lib-favonly" type="checkbox" /> ★ Favorites only</label>
      <span class="chap-range" title="Only novels with this many chapters">
        <select id="lib-len" title="Length">
          <option value="">Any length</option>
          <option value="0-49">Under 50 chapters</option>
          <option value="50-199">50–199 chapters</option>
          <option value="200-499">200–499 chapters</option>
          <option value="500-999">500–999 chapters</option>
          <option value="1000-">1000+ chapters</option>
          <option value="custom">Custom…</option>
        </select>
        <span id="lib-len-custom" class="hidden">
          <input id="lib-len-min" type="number" min="0" placeholder="min" /> –
          <input id="lib-len-max" type="number" min="0" placeholder="max" /> ch
        </span>
      </span>
      <label class="check" title="Show only titles that appear more than once (the same story from different sources); open one and press Merge to combine them"><input id="lib-dupes" type="checkbox" /> Possible duplicates</label>
      <label class="check" title="Hide Boys' Love novels and comics (BL, Yaoi, Shounen Ai, Danmei)"><input id="lib-nobl" type="checkbox" /> Hide BL</label>
      <label class="check" title="Hide machine-translated (MTL) novels"><input id="lib-nomtl" type="checkbox" /> Hide MTL</label>
      <button id="lib-clear" class="hidden" title="Clear every filter">✕ Clear filters</button>
      <select id="lib-views" title="Apply a saved view"><option value="">Saved views</option></select>
      <button id="lib-view-save" title="Save the current sort and filters under a name">💾 Save view</button>
      <button id="lib-view-del" class="hidden" title="Delete the selected saved view">✕</button>
    </div>
    <div id="lib-auto" class="card hidden">
      <div class="shelf-title">🔔 New chapters</div>
      <label class="check"><input id="auto-on" type="checkbox" /> Check for new chapters every day, even with the Reader closed</label>
      <div class="toolbar" style="margin:8px 0 0">
        <label for="auto-scope" style="margin:0">Check</label>
        <select id="auto-scope">
          <option value="both">Favorites and novels I'm reading</option>
          <option value="favorites">Favorites only</option>
          <option value="reading">Novels I'm reading</option>
          <option value="all">Every novel in the library</option>
        </select>
        <label class="check"><input id="auto-dl" type="checkbox" /> Download new chapters too</label>
        <label class="check"><input id="auto-notify" type="checkbox" /> Notify me in this browser</label>
      </div>
      <p class="muted" id="auto-note" style="margin:8px 0 0"></p>
    </div>
    <div id="lib-bmks" class="card hidden">
      <div class="shelf-title">🔖 Bookmarks <span class="muted" id="lib-bmks-count"></span></div>
      <div id="lib-bmks-list"></div>
    </div>
    <p class="muted" id="lib-status">Loading your library…</p>
    <button id="lib-web-btn" class="hidden" title="Look for this on the sources the app can download from"></button>
    <div id="lib-web" class="card hidden">
      <div class="shelf-title">🔎 From the sources <span class="muted" id="lib-web-status"></span>
        <a id="lib-web-stop" class="see-all">Stop</a></div>
      <div id="lib-web-list"></div>
    </div>
    <div id="lib-list"></div>
  </div>

  <div id="view-novel" class="hidden">
    <img id="novel-cover" class="hidden" alt="" />
    <h2 id="novel-title"></h2>
    <p class="meta muted" id="novel-meta"></p>
    <p class="meta muted" id="novel-completeness" aria-live="polite"></p>
    <button id="repair-btn" style="margin-bottom:12px">Retry missing chapters and rebuild EPUB</button>
    <button id="repair-download" class="hidden" style="margin-bottom:12px">Download repaired EPUB</button>
    <p id="repair-status" class="muted" aria-live="polite"></p>
    <div id="novel-tags" class="tags"></div>
    <p id="novel-synopsis"></p>
    <div style="clear:both"></div>
    <button id="continue-btn" class="primary hidden" style="margin-bottom:12px">▶ Continue reading</button>
    <button id="fav-btn" style="margin:0 0 12px 8px" title="Add to favorites">☆ Favorite</button>
    <button id="heal-btn" class="hidden" style="margin:0 0 12px 8px" title="Fill gaps from another copy of this novel in your library">✨ Fill missing chapters</button>
    <button id="deep-btn" class="hidden" style="margin:0 0 12px 8px" title="Search other sites for the missing chapters and download them">🔍 Find missing on other sites</button>
    <button id="merge-btn" class="hidden" style="margin:0 0 12px 8px" title="Combine all copies of this title into the most complete one">⧉ Merge copies</button>
    <button id="del-btn" style="margin:0 0 12px 8px" title="Delete this novel from your library">🗑</button>
    <div class="novel-tools">
      <span id="rate-box" class="rate" title="Your rating"></span>
      <button id="prog-done" class="secondary" title="Count this novel as read to the end">✓ Mark finished</button>
      <button id="prog-unread" class="secondary" title="Forget where you were">↺ Mark unread</button>
      <button id="prog-set" class="secondary" title="Say which chapter you are on (read elsewhere)">✎ Set chapter…</button>
      <button id="shelf-btn" class="secondary" title="Put this novel on your shelves">📚 Shelves</button>
    </div>
    <div id="shelf-box" class="tags hidden"></div>
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
    schedulePush();
  }
  function saveScroll(novelId, chapterId, frac) {
    var p = loadPos();
    var e = p[novelId];
    if (!e || typeof e === "string" || e.id !== chapterId) return;
    e.scroll = frac; e.ts = Date.now();
    localStorage.setItem(POS, JSON.stringify(p));
    schedulePush();
  }
  function loadUpdates() { try { return JSON.parse(localStorage.getItem(UPD) || "{}"); } catch (e) { return {}; } }
  function saveUpdates(m) { localStorage.setItem(UPD, JSON.stringify(m)); touchSync("updates"); }
  function loadPrefs() { try { return JSON.parse(localStorage.getItem(PREFS) || "{}"); } catch (e) { return {}; } }
  function savePrefs(p) { localStorage.setItem(PREFS, JSON.stringify(p)); touchSync("prefs"); }
  function loadFavs() { try { return JSON.parse(localStorage.getItem(FAV) || "[]"); } catch (e) { return []; } }
  function saveFavs(a) { localStorage.setItem(FAV, JSON.stringify(a)); touchSync("favs"); }
  function loadBmks() { try { return JSON.parse(localStorage.getItem(BMK) || "{}"); } catch (e) { return {}; } }
  function saveBmks(m) { localStorage.setItem(BMK, JSON.stringify(m)); touchSync("bookmarks"); }
  function loadView() { try { return JSON.parse(localStorage.getItem(VIEW) || "{}"); } catch (e) { return {}; } }
  // The sort and every filter, as one object: what is remembered between
  // visits, and what a saved view holds.
  function currentView() {
    return {
      sort: els["lib-sort"].value, kind: els["lib-kind"].value, cat: lib.cat,
      tag: els["lib-tag"].value, source: els["lib-source"].value,
      lang: els["lib-lang"].value, state: els["lib-state"].value, progress: els["lib-progress"].value,
      favonly: els["lib-favonly"].checked, dupes: els["lib-dupes"].checked, shelf: els["lib-shelf"].value,
      nobl: els["lib-nobl"].checked, len: els["lib-len"].value, lenMin: els["lib-len-min"].value,
      lenMax: els["lib-len-max"].value, nomtl: els["lib-nomtl"].checked
    };
  }
  function applyView(v) {
    v = v || {};
    els["lib-sort"].value = v.sort || "recent";
    els["lib-kind"].value = v.kind || "";
    els["lib-tag"].value = v.tag || "";
    els["lib-state"].value = v.state || "";
    els["lib-progress"].value = v.progress || "";
    els["lib-favonly"].checked = !!(v.favonly || v.cat === "__fav");
    els["lib-dupes"].checked = !!v.dupes;
    lib.pendingShelf = v.shelf || "";
    if (lib.shelves) { els["lib-shelf"].value = v.shelf || ""; }
    els["lib-nobl"].checked = !!v.nobl;
    els["lib-nomtl"].checked = !!v.nomtl;
    els["lib-len"].value = v.len || "";
    els["lib-len-min"].value = v.lenMin || "";
    els["lib-len-max"].value = v.lenMax || "";
    els["lib-len-custom"].classList.toggle("hidden", els["lib-len"].value !== "custom");
    lib.cat = (v.cat && v.cat !== "__fav") ? v.cat : "";
    // source and language options exist only once the library has loaded
    lib.pending = { source: v.source || "", lang: v.lang || "" };
    if (lib.all.length) renderFilterOptions();
  }
  function saveView() { localStorage.setItem(VIEW, JSON.stringify(currentView())); }

  // ---- Saved views ----
  var VIEWS = "lncrawl_reader_views";  // { name: view }
  function loadViews() { try { return JSON.parse(localStorage.getItem(VIEWS) || "{}"); } catch (e) { return {}; } }
  function saveViews(m) { localStorage.setItem(VIEWS, JSON.stringify(m)); touchSync("views"); }

  // ---- Sync with the server ----
  // Positions, favorites, bookmarks and saved views live in this browser and are
  // copied to the account, so every device reading from this server shares them.
  // Positions merge novel by novel; the other sections go to whichever device
  // changed them last, which is what the timestamps below record.
  var SYNC_TS = "lncrawl_reader_sync_ts";  // { favs, bookmarks, views: last change ts }
  var RATE = "lncrawl_reader_ratings";  // { novelId: 1-5 }
  var AUTO = "lncrawl_reader_autoupdate";  // { enabled, scope, download, notify }
  function loadRatings() { try { return JSON.parse(localStorage.getItem(RATE) || "{}"); } catch (e) { return {}; } }
  function saveRatings(m) { localStorage.setItem(RATE, JSON.stringify(m)); touchSync("ratings"); }
  function loadAuto() { try { return JSON.parse(localStorage.getItem(AUTO) || "{}"); } catch (e) { return {}; } }
  function saveAuto(m) { localStorage.setItem(AUTO, JSON.stringify(m)); touchSync("autoupdate"); }
  var SECTIONS = {
    prefs: { load: function () { return loadPrefs(); }, key: function () { return PREFS; } },
    updates: { load: function () { return loadUpdates(); }, key: function () { return UPD; } },
    ratings: { load: function () { return loadRatings(); }, key: function () { return RATE; } },
    autoupdate: { load: function () { return loadAuto(); }, key: function () { return AUTO; } },
    favs: { load: function () { return loadFavs(); }, key: function () { return FAV; } },
    bookmarks: { load: function () { return loadBmks(); }, key: function () { return BMK; } },
    views: { load: function () { return loadViews(); }, key: function () { return VIEWS; } }
  };
  function loadSyncTs() { try { return JSON.parse(localStorage.getItem(SYNC_TS) || "{}"); } catch (e) { return {}; } }
  function touchSync(section) {
    var t = loadSyncTs(); t[section] = Date.now();
    localStorage.setItem(SYNC_TS, JSON.stringify(t));
    schedulePush();
  }
  var pushTimer = null;
  function schedulePush() {
    if (!token()) return;
    clearTimeout(pushTimer);
    pushTimer = setTimeout(pushState, 2500);
  }
  function statePayload() {
    var t = loadSyncTs();
    var out = { pos: {} };
    var p = loadPos();
    Object.keys(p).forEach(function (id) { if (p[id] && typeof p[id] === "object") out.pos[id] = p[id]; });
    Object.keys(SECTIONS).forEach(function (name) {
      out[name] = { data: SECTIONS[name].load(), ts: t[name] || 0 };
    });
    return out;
  }
  function isEmpty(v) { return !v || (Array.isArray(v) ? !v.length : !Object.keys(v).length); }
  function union(name, a, b) {
    if (name === "favs") return a.concat(b.filter(function (x) { return a.indexOf(x) < 0; }));
    var out = JSON.parse(JSON.stringify(b || {}));
    Object.keys(a || {}).forEach(function (k) {
      if (name === "bookmarks") {
        var have = out[k] || [];
        (a[k] || []).forEach(function (bm) {
          if (!have.some(function (x) { return x.id === bm.id && x.ts === bm.ts; })) have.push(bm);
        });
        out[k] = have;
      } else {
        out[k] = a[k];
      }
    });
    return out;
  }
  // Take in the server's copy. Returns true when this device has something the
  // server lacks, so it should send its copy back.
  function absorbState(server, first) {
    if (!server || typeof server !== "object") return false;
    var dirty = false;
    var p = loadPos();
    Object.keys(server.pos || {}).forEach(function (id) {
      var mine = p[id] && typeof p[id] === "object" ? p[id] : null;
      var theirs = server.pos[id];
      if (!mine || (theirs.ts || 0) > (mine.ts || 0)) p[id] = theirs;
    });
    Object.keys(p).forEach(function (id) {
      var theirs = (server.pos || {})[id];
      if (p[id] && typeof p[id] === "object" && (!theirs || (p[id].ts || 0) > (theirs.ts || 0))) dirty = true;
    });
    localStorage.setItem(POS, JSON.stringify(p));
    var t = loadSyncTs();
    Object.keys(SECTIONS).forEach(function (name) {
      var theirs = server[name];
      var mine = SECTIONS[name].load();
      var mineTs = t[name] || 0;
      if (!theirs || !("data" in theirs)) { if (!isEmpty(mine)) dirty = true; return; }
      if (first && !mineTs && !isEmpty(mine)) {
        // kept on this device before syncing existed: add it, lose nothing
        localStorage.setItem(SECTIONS[name].key(), JSON.stringify(union(name, mine, theirs.data)));
        t[name] = Date.now(); dirty = true;
      } else if ((theirs.ts || 0) > mineTs) {
        localStorage.setItem(SECTIONS[name].key(), JSON.stringify(theirs.data));
        t[name] = theirs.ts;
      } else if (mineTs > (theirs.ts || 0)) {
        dirty = true;
      }
    });
    localStorage.setItem(SYNC_TS, JSON.stringify(t));
    return dirty;
  }
  function pushState() {
    api("/settings/reader-state", { method: "PUT", body: JSON.stringify(statePayload()) })
      .then(function (merged) { absorbState(merged, false); })
      .catch(function () { /* offline: kept here and sent next time */ });
  }
  function syncState() {
    return api("/settings/reader-state", { method: "GET" })
      .then(function (server) {
        if (absorbState(server, true)) pushState();
        renderViews();
        prefs = loadPrefs();  // another device may have changed the reading settings
        applyPrefs();
        renderAuto();
        if (lib.all.length) { renderContinue(); renderLibrary(); }
      })
      .catch(function () { /* offline: this device's copy stays in use */ });
  }

  var els = {};
  ["nav-back","search","view-library","lib-continue-wrap","lib-continue","lib-continue-all","lib-sort","lib-kind","lib-tag","lib-tag-list","lib-source","lib-lang","lib-progress","lib-state","lib-favonly","lib-len","lib-len-custom","lib-len-min","lib-len-max","lib-nobl","lib-dupes","lib-shelf","lib-auto-btn","lib-auto","auto-on","auto-scope","auto-dl","auto-notify","auto-note","rate-box","prog-done","prog-unread","prog-set","shelf-btn","shelf-box","lib-fill","lib-views","lib-view-save","lib-view-del","lib-web-btn","lib-web","lib-web-status","lib-web-stop","lib-web-list","lib-bmks-btn","lib-bmks","lib-bmks-count","lib-bmks-list","lib-nomtl","lib-clear","lib-cats",
   "lib-update","lib-status","lib-list","view-novel","novel-cover","novel-title","novel-meta","novel-tags",
   "novel-synopsis","continue-btn","heal-btn","heal-msg","chap-list","chap-more-wrap","chap-more",
   "view-reader","reader-title","reader-content","prev-btn","next-btn","reader-pos","font-up",
   "font-dn","auth","email","password","login","auth-msg","search-wrap","aa-btn","aa-panel",
   "aa-theme","aa-font","aa-width","aa-trans","fit-btn","fav-btn","bmk-btn","bmk-list",
   "trans-pop","lib-select","lib-del","merge-btn","del-btn","deep-btn",
   "novel-completeness","repair-btn","repair-status","repair-download"].forEach(function (id) {
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
  // Covers load when their card scrolls into view and are kept, so a large
  // library doesn't fetch every cover up front or again on each filter change.
  var coverCache = {};
  var coverWatch = ("IntersectionObserver" in window) ? new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (!e.isIntersecting) return;
      coverWatch.unobserve(e.target);
      fetchCover(e.target, e.target.dataset.novel);
    });
  }, { rootMargin: "300px" }) : null;
  function fetchCover(img, novelId) {
    var hit = coverCache[novelId];
    if (hit) { img.src = hit; img.classList.remove("hidden"); return; }
    fetch("/api/novel/" + novelId + "/cover", { headers: { "Authorization": "Bearer " + token() } })
      .then(function (r) { if (!r.ok) throw new Error("cover"); return r.blob(); })
      .then(function (b) {
        coverCache[novelId] = URL.createObjectURL(b);
        img.src = coverCache[novelId]; img.classList.remove("hidden");
      })
      .catch(function () { /* leave placeholder */ });
  }
  function loadCover(img, novelId) {
    if (coverCache[novelId] || !coverWatch) { fetchCover(img, novelId); return; }
    img.dataset.novel = novelId;
    coverWatch.observe(img);
  }

  // The server hands out at most 100 novels a request, so the library is read a
  // page at a time until every novel is in, however large it has grown.
  function fetchAllNovels(q) {
    var all = [];
    function page(offset) {
      return api("/novels?limit=100&offset=" + offset + "&search=" + encodeURIComponent(q || ""))
        .then(function (res) {
          var items = (res && res.items) || [];
          all = all.concat(items);
          var total = (res && res.total) || 0;
          if (items.length === 100 && all.length < total) {
            els["lib-status"].textContent = "Loading your library… " + all.length + " of " + total;
            return page(offset + items.length);
          }
          return all;
        });
    }
    return page(0);
  }

  // The whole library is loaded once and searched here, so a search can look at
  // every field, forgive punctuation, and still leave the shelf and filters whole.
  function loadLibrary() {
    els["lib-status"].textContent = "Loading your library…";
    els["lib-list"].innerHTML = "";
    api("/novels/latest-chapters").then(function (m) {
      lib.latest = m || {};
      if (lib.all.length) { renderLibrary(); notifyNew(); }
    }).catch(function () { lib.latest = {}; });
    loadShelves();
    fetchAllNovels("").then(function (items) {
      lib.all = items;
      lib.everything = items;
      lib.hay = {};
      renderContinue();
      renderFilterOptions();
      renderCategories();
      renderLibrary();
      if (lib.latest) notifyNew();
    }).catch(function (e) { els["lib-status"].textContent = "Couldn't load library: " + e.message; });
  }

  // A "Continue reading" shelf of the most recently opened novels.
  function timeAgo(ts) {
    if (!ts) return "";
    var s = Math.max(0, (Date.now() - ts) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    if (s < 86400 * 30) return Math.floor(s / 86400) + "d ago";
    return new Date(ts).toLocaleDateString();
  }

  // Novels being read, last read first. Finished ones drop off until they gain
  // chapters; the full list is one click away as the "Reading" filter.
  var CONTINUE_MAX = 20;
  function renderContinue() {
    var byId = {}; (lib.everything || lib.all).forEach(function (n) { byId[n.id] = n; });
    var recent = [];
    var posMap = loadPos();
    Object.keys(posMap).forEach(function (id) {
      var n = byId[id]; var pe = posOf(id);
      if (n && pe && progressOf(n) !== "finished") recent.push({ n: n, pe: pe });
    });
    recent.sort(function (a, b) { return (b.pe.ts || 0) - (a.pe.ts || 0); });
    var total = recent.length;
    recent = recent.slice(0, CONTINUE_MAX);
    els["lib-continue"].innerHTML = "";
    els["lib-continue-wrap"].classList.toggle("hidden", !recent.length);
    els["lib-continue-all"].textContent = total > CONTINUE_MAX ? "See all (" + total + ") ›" : (total > 1 ? "See all ›" : "");
    recent.forEach(function (r) {
      var mini = document.createElement("div"); mini.className = "mini";
      mini.title = (r.n.title || "") + (r.pe.ts ? " · last read " + new Date(r.pe.ts).toLocaleString() : "");
      var img = document.createElement("img"); img.alt = "";
      if (r.n.cover_available) loadCover(img, r.n.id);
      var t = document.createElement("div"); t.className = "mt"; t.textContent = r.n.title || "Untitled";
      var meta = document.createElement("div"); meta.className = "mm";
      meta.textContent = [
        r.pe.serial ? "Ch " + r.pe.serial + (r.n.chapter_count ? " / " + r.n.chapter_count : "") : "",
        timeAgo(r.pe.ts)
      ].filter(Boolean).join(" · ");
      mini.appendChild(img); mini.appendChild(t); mini.appendChild(meta);
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

  // What kind of comic a novel is. The library only records manga or not, so
  // the kind comes from its tags, then from its original language.
  function mediaType(n) {
    if (!n.manga) return "novel";
    var tags = (n.tags || []).join(" ").toLowerCase();
    if (tags.indexOf("manhwa") >= 0) return "manhwa";
    if (tags.indexOf("manhua") >= 0) return "manhua";
    var lang = (n.language || "").toLowerCase().slice(0, 2);
    if (lang === "ko") return "manhwa";
    if (lang === "zh") return "manhua";
    return "manga";
  }

  var LANG_NAMES = { en: "English", ja: "Japanese", ko: "Korean", zh: "Chinese", es: "Spanish",
    fr: "French", pt: "Portuguese", ru: "Russian", id: "Indonesian", vi: "Vietnamese", de: "German",
    it: "Italian", ar: "Arabic", th: "Thai", tr: "Turkish", pl: "Polish" };

  function fillSelect(sel, values, label, keep) {
    var first = sel.options[0].textContent;
    sel.innerHTML = "";
    var all = document.createElement("option"); all.value = ""; all.textContent = first; sel.appendChild(all);
    values.forEach(function (v) {
      var o = document.createElement("option"); o.value = v[0];
      o.textContent = label(v[0]) + " (" + v[1] + ")"; sel.appendChild(o);
    });
    sel.value = values.some(function (v) { return v[0] === keep; }) ? keep : "";
  }

  function counted(key) {
    var counts = {};
    lib.all.forEach(function (n) { var k = key(n); if (k) counts[k] = (counts[k] || 0) + 1; });
    return Object.keys(counts).sort().map(function (k) { return [k, counts[k]]; });
  }

  function renderFilterOptions() {
    var pending = lib.pending || {};
    fillSelect(els["lib-source"], counted(function (n) { return n.domain; }),
      function (d) { return d; }, pending.source || els["lib-source"].value);
    fillSelect(els["lib-lang"], counted(function (n) { return (n.language || "").slice(0, 2).toLowerCase(); }),
      function (l) { return LANG_NAMES[l] || l; }, pending.lang || els["lib-lang"].value);
    lib.pending = null;
    var tags = {};
    buildTagLabels();
    lib.all.forEach(function (n) { tagsOf(n).forEach(function (t) { tags[t] = (tags[t] || 0) + 1; }); });
    els["lib-tag-list"].innerHTML = "";
    Object.keys(tags).sort(function (a, b) { return tags[b] - tags[a] || a.localeCompare(b); })
      .forEach(function (t) {
        var o = document.createElement("option"); o.value = t; o.label = tags[t] + " novel(s)";
        els["lib-tag-list"].appendChild(o);
      });
  }

  // "romance, -harem": every plain term must match one of a novel's tags (in
  // part, ignoring case), and no term starting with - may match any of them.
  function tagTerms() {
    return els["lib-tag"].value.split(",").map(function (t) { return t.trim().toLowerCase(); })
      .filter(Boolean);
  }

  function matchesTags(n, terms) {
    var tags = tagsOf(n).concat(n.tags || []).map(function (t) { return String(t).toLowerCase(); });
    var has = function (term) { return tags.some(function (t) { return t.indexOf(term) >= 0; }); };
    return terms.every(function (term) {
      if (term.charAt(0) === "-") return term.length === 1 || !has(term.slice(1).trim());
      return has(term);
    });
  }

  // completed / ongoing / hiatus / cancelled, as the server worked it out when
  // the novel was last fetched from its source; null when the source never said
  function statusOf(n) { return (n.extra && n.extra.status) || null; }

  // Reading progress from the position saved on this device: never opened,
  // part-way, or on the last chapter and scrolled to its end. A finished novel
  // that gains chapters counts as being read again.
  function progressOf(n) {
    var pe = posOf(n.id);
    if (!pe) return "unread";
    var total = n.chapter_count || 0;
    if (pe.serial && total && pe.serial >= total && (pe.scroll || 0) >= 0.9) return "finished";
    return "reading";
  }

  // Sort orders for status; a novel whose source never said comes last either way.
  var ONGOING_FIRST = ["ongoing", "hiatus", "completed", "cancelled"];
  var COMPLETED_FIRST = ["completed", "ongoing", "hiatus", "cancelled"];
  function statusRank(n, order) {
    var i = order.indexOf(statusOf(n));
    return i < 0 ? order.length : i;
  }

  // Letters and digits only, lower case, accents dropped: "Solo Leveling:
  // Ragnarok" and "solo leveling ragnarok" read the same.
  function norm(s) {
    return String(s || "").normalize("NFKD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase()
      .replace(/[^\\p{L}\\p{N}]+/gu, " ").trim();
  }
  function altTitles(n) { return (n.extra && n.extra.alt_titles) || []; }
  // What a search looks through, per novel, worked out once per load.
  function haystack(n) {
    lib.hay = lib.hay || {};
    var h = lib.hay[n.id];
    if (!h) {
      h = lib.hay[n.id] = {
        title: norm([n.title].concat(altTitles(n)).join(" | ")),
        author: norm(n.authors),
        rest: norm([n.synopsis, (n.tags || []).join(" "), n.domain].join(" "))
      };
    }
    return h;
  }
  // 0 = title, 1 = author, 2 = description, tags or source, -1 = no match.
  // Every word must appear somewhere; "sololeveling" also finds "Solo Leveling".
  function searchRank(n, words, joined) {
    var h = haystack(n);
    var all = h.title + " " + h.author + " " + h.rest;
    var hit = words.every(function (w) { return all.indexOf(w) >= 0; }) ||
      all.replace(/ /g, "").indexOf(joined) >= 0;
    if (!hit) return -1;
    if (words.every(function (w) { return h.title.indexOf(w) >= 0; }) ||
        h.title.replace(/ /g, "").indexOf(joined) >= 0) return 0;
    if (words.every(function (w) { return h.author.indexOf(w) >= 0; })) return 1;
    return 2;
  }
  // Edit distance, giving up past `max` (cheap for short words).
  function within(a, b, max) {
    if (Math.abs(a.length - b.length) > max) return false;
    var prev = [], i, j;
    for (j = 0; j <= b.length; j++) prev[j] = j;
    for (i = 1; i <= a.length; i++) {
      var cur = [i], best = i;
      for (j = 1; j <= b.length; j++) {
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
        if (cur[j] < best) best = cur[j];
      }
      if (best > max) return false;
      prev = cur;
    }
    return prev[b.length] <= max;
  }
  // Like searchRank, but each word of four or more letters may be one letter
  // off (two from eight letters) against a word of the title or author.
  function fuzzyRank(n, words) {
    var h = haystack(n);
    var titleWords = h.title.split(" "), authorWords = h.author.split(" ");
    var near = function (w, pool) {
      if (w.length < 4) return pool.some(function (x) { return x.indexOf(w) >= 0; });
      var max = w.length >= 8 ? 2 : 1;
      return pool.some(function (x) { return x.indexOf(w) >= 0 || within(w, x, max); });
    };
    if (words.every(function (w) { return near(w, titleWords); })) return 0;
    if (words.every(function (w) { return near(w, titleWords.concat(authorWords)); })) return 1;
    return -1;
  }

  // The same story under the same title from different sources.
  function dupKey(n) { return norm(n.title).replace(/^the /, "").replace(/ /g, ""); }

  // Sources spell the same tag differently ("BL", "Boys' Love", "boys love";
  // "Sci-fi", "Science Fiction"). Each is filed under one key and shown under
  // the spelling the library uses most.
  var TAG_SYNONYMS = {
    bl: "boyslove", boylove: "boyslove", shounenai: "boyslove", shonenai: "boyslove",
    gl: "girlslove", girllove: "girlslove", shoujoai: "girlslove", shojoai: "girlslove",
    scifi: "sciencefiction", sf: "sciencefiction",
    shonen: "shounen", shojo: "shoujo",
    genderswap: "genderbender", rofan: "romancefantasy", romanticfantasy: "romancefantasy",
    martialart: "martialarts", wuxia: "wuxia", xianxia: "xianxia",
    sliceoflifes: "sliceoflife", webcomic: "webtoon", webtoonwebcomic: "webtoon",
    adventures: "adventure", comedies: "comedy", dramas: "drama", romances: "romance"
  };
  function tagKey(t) {
    var k = norm(t).replace(/ /g, "");
    return TAG_SYNONYMS[k] || k;
  }
  function buildTagLabels() {
    var seen = {};
    lib.all.forEach(function (n) {
      (n.tags || []).forEach(function (t) {
        var k = tagKey(t);
        if (!k) return;
        var spell = seen[k] || (seen[k] = {});
        spell[t] = (spell[t] || 0) + 1;
      });
    });
    lib.tagLabel = {};
    Object.keys(seen).forEach(function (k) {
      // most used; on a tie, the one written with capitals ("Boys' Love", not "bl")
      var caps = function (t) { return (t.match(/[A-Z]/g) || []).length > 0 ? 1 : 0; };
      lib.tagLabel[k] = Object.keys(seen[k]).sort(function (a, b) {
        return (seen[k][b] - seen[k][a]) || (caps(b) - caps(a)) || (b.length - a.length);
      })[0];
    });
    lib.tagCache = {};
  }
  function tagsOf(n) {
    lib.tagCache = lib.tagCache || {};
    var hit = lib.tagCache[n.id];
    if (hit) return hit;
    var out = [];
    (n.tags || []).forEach(function (t) {
      var k = tagKey(t);
      var label = (lib.tagLabel || {})[k] || t;
      if (k && out.indexOf(label) < 0) out.push(label);
    });
    return (lib.tagCache[n.id] = out);
  }

  // Boys' Love goes by several names across sources. Tags are compared whole,
  // letters only, so "BL" matches but "Blood" or "Black Clover" do not.
  var BL_TAGS = ["bl", "boyslove", "boylove", "yaoi", "shounenai", "shonenai", "danmei", "bara"];
  function isBL(n) {
    return (n.tags || []).some(function (t) {
      return BL_TAGS.indexOf(String(t).toLowerCase().replace(/[^a-z]/g, "")) >= 0;
    });
  }

  // [min, max] chapters from the Length filter, or null when it is off
  function chapterRange() {
    var v = els["lib-len"].value;
    if (!v) return null;
    var lo = "", hi = "";
    if (v === "custom") { lo = els["lib-len-min"].value; hi = els["lib-len-max"].value; }
    else { var parts = v.split("-"); lo = parts[0]; hi = parts[1]; }
    var min = lo === "" ? 0 : Math.max(0, parseInt(lo, 10) || 0);
    var max = hi === "" ? Infinity : Math.max(0, parseInt(hi, 10) || 0);
    if (!min && max === Infinity) return null;
    return [min, max];
  }

  function filtersActive() {
    return !!(els["lib-kind"].value || els["lib-tag"].value.trim() || els["lib-source"].value ||
      els["lib-state"].value || els["lib-progress"].value || els["lib-favonly"].checked || !!chapterRange() || !!els["lib-shelf"].value || els["lib-nobl"].checked || els["lib-dupes"].checked ||
      els["lib-lang"].value || els["lib-nomtl"].checked || lib.cat);
  }

  function renderCategories() {
    var counts = {};
    lib.all.forEach(function (n) {
      tagsOf(n).forEach(function (t) { counts[t] = (counts[t] || 0) + 1; });
    });
    var top = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; }).slice(0, 12);
    els["lib-cats"].innerHTML = "";
    var chips = top.map(function (t) { return { v: t, t: t }; });
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
    renderAllBookmarks();  // keeps the Bookmarks count current after reading
    updateWebButton();
    updateFillButton();
    renderAuto();
    var items = lib.all.slice();
    var q = els["search"].value;
    var words = norm(q).split(" ").filter(Boolean);
    var joined = words.join("");
    var rank = {};
    var fuzzy = false;
    if (words.length) {
      var exact = items.filter(function (n) {
        var r = searchRank(n, words, joined);
        rank[n.id] = r;
        return r >= 0;
      });
      if (!exact.length) {
        // nothing exact: allow a slip of the keyboard per word
        exact = items.filter(function (n) {
          var r = fuzzyRank(n, words);
          rank[n.id] = r;
          return r >= 0;
        });
        fuzzy = exact.length > 0;
      }
      items = exact;
    }
    var dupes = els["lib-dupes"].checked;
    if (dupes) {
      var count = {};
      (lib.everything || lib.all).forEach(function (n) { var k = dupKey(n); count[k] = (count[k] || 0) + 1; });
      items = items.filter(function (n) { return count[dupKey(n)] > 1; });
    }
    var shelf = els["lib-shelf"].value;
    if (shelf) {
      var onShelf = (lib.membership || {})[shelf] || [];
      items = items.filter(function (n) { return onShelf.indexOf(n.id) >= 0; });
    }
    if (els["lib-favonly"].checked) {
      var favs = loadFavs();
      items = items.filter(function (n) { return favs.indexOf(n.id) >= 0; });
    }
    if (lib.cat) {
      items = items.filter(function (n) { return tagsOf(n).indexOf(lib.cat) >= 0; });
    }

    var kind = els["lib-kind"].value;
    if (kind === "comic") items = items.filter(function (n) { return n.manga; });
    else if (kind) items = items.filter(function (n) { return mediaType(n) === kind; });
    var terms = tagTerms();
    if (terms.length) items = items.filter(function (n) { return matchesTags(n, terms); });
    var source = els["lib-source"].value;
    if (source) items = items.filter(function (n) { return n.domain === source; });
    var lang = els["lib-lang"].value;
    if (lang) items = items.filter(function (n) { return (n.language || "").slice(0, 2).toLowerCase() === lang; });
    var progress = els["lib-progress"].value;
    if (progress) items = items.filter(function (n) { return progressOf(n) === progress; });
    var state = els["lib-state"].value;
    if (state) items = items.filter(function (n) { return (statusOf(n) || "unknown") === state; });
    var range = chapterRange();
    if (range) {
      items = items.filter(function (n) {
        var c = n.chapter_count || 0;
        return c >= range[0] && c <= range[1];
      });
    }
    if (els["lib-nobl"].checked) items = items.filter(function (n) { return !isBL(n); });
    if (els["lib-nomtl"].checked) items = items.filter(function (n) { return !n.mtl; });
    els["lib-clear"].classList.toggle("hidden", !filtersActive());

    var sort = els["lib-sort"].value;
    var byText = function (a, b, key) {
      // empty values last, then natural order so "Vol 2" comes before "Vol 10"
      var x = a[key] || "", y = b[key] || "";
      if (!x !== !y) return x ? -1 : 1;
      return x.localeCompare(y, undefined, { numeric: true, sensitivity: "base" });
    };
    var readAt = function (n) { var p = posOf(n.id); return (p && p.ts) || 0; };
    items.sort(function (a, b) {
      // a search puts title matches first; possible duplicates sit together
      if (words.length && rank[a.id] !== rank[b.id]) return rank[a.id] - rank[b.id];
      if (dupes) {
        var ka = dupKey(a), kb = dupKey(b);
        if (ka !== kb) return ka < kb ? -1 : 1;
      }
      var d = 0;
      if (sort === "title") d = byText(a, b, "title");
      else if (sort === "title-desc") d = -byText(a, b, "title");
      else if (sort === "chapters") d = (b.chapter_count || 0) - (a.chapter_count || 0);
      else if (sort === "chapters-asc") d = (a.chapter_count || 0) - (b.chapter_count || 0);
      else if (sort === "updated") d = (b.updated_at || 0) - (a.updated_at || 0);
      else if (sort === "oldest") d = (a.created_at || 0) - (b.created_at || 0);
      else if (sort === "read") d = readAt(b) - readAt(a);
      else if (sort === "author") d = byText(a, b, "authors");
      else if (sort === "source") d = byText(a, b, "domain");
      else if (sort === "rating") d = (loadRatings()[b.id] || 0) - (loadRatings()[a.id] || 0);
      else if (sort === "newch") d = ((lib.latest || {})[b.id] || 0) - ((lib.latest || {})[a.id] || 0);
      else if (sort === "status") d = statusRank(a, ONGOING_FIRST) - statusRank(b, ONGOING_FIRST);
      else if (sort === "status-done") d = statusRank(a, COMPLETED_FIRST) - statusRank(b, COMPLETED_FIRST);
      else d = (b.created_at || 0) - (a.created_at || 0);  // recent
      return d || byText(a, b, "title");
    });

    els["lib-list"].innerHTML = "";
    if (!items.length) {
      els["lib-status"].textContent = lib.all.length
        ? (words.length ? "Nothing in your library matches “" + q.trim() + "”." : "No matches.")
        : "Your library is empty — download something first (Tools).";
      return;
    }
    els["lib-status"].textContent = items.length + " novel(s)" +
      (words.length ? (fuzzy ? " close to “" + q.trim() + "” (no exact match)" : " matching “" + q.trim() + "”") : "") +
      (dupes ? " · possible duplicates — open one and press Merge to combine them" : "") +
      (kind ? " · " + els["lib-kind"].selectedOptions[0].textContent : "") +
      (terms.length ? " · tags: " + terms.join(", ") : "") +
      (state ? " · " + els["lib-state"].selectedOptions[0].textContent : "") +
      (progress ? " · " + els["lib-progress"].selectedOptions[0].textContent : "") +
      (range ? " · " + (range[1] === Infinity ? range[0] + "+" : range[0] + "–" + range[1]) + " chapters" : "") +
      (els["lib-favonly"].checked ? " · ★ Favorites" : "") +
      (lib.cat ? " · " + lib.cat : "");
    // Cards are made a batch at a time as the list is scrolled, so a library of
    // thousands stays quick to filter.
    var token_ = lib.renderToken = (lib.renderToken || 0) + 1;
    var shown = 0;
    function more() {
      if (token_ !== lib.renderToken) return;
      items.slice(shown, shown + CARD_BATCH).forEach(buildCard);
      shown = Math.min(items.length, shown + CARD_BATCH);
      if (shown < items.length) {
        var sentinel = document.createElement("div"); sentinel.className = "lib-more";
        els["lib-list"].appendChild(sentinel);
        if ("IntersectionObserver" in window) {
          var io = new IntersectionObserver(function (entries) {
            if (!entries[0].isIntersecting) return;
            io.disconnect(); sentinel.remove(); more();
          }, { rootMargin: "600px" });
          io.observe(sentinel);
        } else { sentinel.remove(); more(); }
      }
    }
    more();
  }

  var CARD_BATCH = 60;
  function buildCard(n) {
    {
      var c = document.createElement("div");
      c.className = "card libcard";

      var img = document.createElement("img"); img.className = "cover hidden"; img.alt = "";
      if (n.cover_available) loadCover(img, n.id);

      var body = document.createElement("div"); body.className = "body";
      var h = document.createElement("h3"); h.textContent = n.title || "Untitled";
      var star = document.createElement("span");
      star.className = "fav-star" + (isFav(n.id) ? " on" : "");
      star.textContent = isFav(n.id) ? "★" : "☆";
      star.title = isFav(n.id) ? "Remove from favorites" : "Add to favorites";
      star.addEventListener("click", function (e) {
        e.stopPropagation();
        toggleFav(n.id);
        renderLibrary();
      });
      if (n.manga) { var bd = document.createElement("span"); bd.className = "badge"; bd.textContent = "Manga"; h.appendChild(bd); }
      if (hasNewChapters(n)) {
        var fb = document.createElement("span"); fb.className = "badge fresh";
        fb.textContent = "● new chapters"; fb.title = "Chapters were added since you last read it";
        h.appendChild(fb);
      }
      var st8 = statusOf(n);
      if (st8 && st8 !== "ongoing") {
        var sb = document.createElement("span"); sb.className = "badge";
        sb.textContent = { completed: "Completed", hiatus: "Hiatus", cancelled: "Cancelled" }[st8] || st8;
        h.appendChild(sb);
      }
      var upd = loadUpdates()[n.id];
      if (upd && upd.found) {
        var nb = document.createElement("span"); nb.className = "badge new";
        nb.textContent = "+" + upd.found + " new"; h.appendChild(nb);
      }
      var m = document.createElement("div"); m.className = "meta";
      var newest = (lib.latest || {})[n.id];
      var stars = loadRatings()[n.id];
      m.textContent = [stars ? "★" + stars : "", n.authors || "", (n.chapter_count || 0) + " chapters", n.domain || "",
        newest ? "new chapter " + timeAgo(newest) : ""]
        .filter(Boolean).join("  ·  ");
      h.insertBefore(star, h.firstChild);
      body.appendChild(h); body.appendChild(m);

      var pe = posOf(n.id);
      if (pe) {
        var line = document.createElement("div"); line.className = "meta";
        line.style.color = "#7aa2ff";
        line.textContent = (progressOf(n) === "finished" ? "✓ Finished" : "▶ Resume") +
          (pe.serial ? (" · ch " + pe.serial + (n.chapter_count ? " / " + n.chapter_count : "")) : "");
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
        // long descriptions are cut to two lines; "more" reads the rest in place
        if (d.textContent.length > 160) {
          var more = document.createElement("span"); more.className = "desc-more"; more.textContent = "more";
          more.addEventListener("click", function (e) {
            e.stopPropagation();
            var open = d.classList.toggle("open");
            more.textContent = open ? "less" : "more";
          });
          body.appendChild(more);
        }
      }
      if (tagsOf(n).length) {
        var tg = document.createElement("div"); tg.className = "tags";
        tagsOf(n).slice(0, 5).forEach(function (t) {
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
    }
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
    var failed = 0;
    var reason = "";
    (function step() {
      if (i >= ids.length) {
        btn.disabled = false;
        lib.manage = false; lib.sel = {};
        els["lib-select"].textContent = "☑ Select";
        updateDelBtn();
        loadLibrary(els["search"].value.trim());
        // a refusal must not look like nothing happened
        if (failed) alert(failed + " of " + ids.length + " novel(s) could not be deleted: " + reason);
        return;
      }
      var id = ids[i++];
      api("/novel/" + id, { method: "DELETE" }).then(step).catch(function (e) {
        failed += 1;
        reason = reason || e.message;
        step();
      });
    })();
  });

  els["lib-sort"].addEventListener("change", renderLibrary);
  els["lib-kind"].addEventListener("change", renderLibrary);
  els["lib-source"].addEventListener("change", renderLibrary);
  els["lib-lang"].addEventListener("change", renderLibrary);
  els["lib-state"].addEventListener("change", renderLibrary);
  els["lib-progress"].addEventListener("change", renderLibrary);
  els["lib-continue-all"].addEventListener("click", function () {
    els["lib-progress"].value = "reading";
    els["lib-sort"].value = "read";
    renderLibrary();
    els["lib-status"].scrollIntoView({ behavior: "smooth", block: "start" });
  });
  els["lib-favonly"].addEventListener("change", renderLibrary);
  els["lib-dupes"].addEventListener("change", renderLibrary);
  els["lib-shelf"].addEventListener("change", renderLibrary);
  els["lib-nobl"].addEventListener("change", renderLibrary);
  els["lib-len"].addEventListener("change", function () {
    els["lib-len-custom"].classList.toggle("hidden", this.value !== "custom");
    renderLibrary();
  });
  ["lib-len-min", "lib-len-max"].forEach(function (k) {
    els[k].addEventListener("input", function () { clearTimeout(tagTimer); tagTimer = setTimeout(renderLibrary, 200); });
  });
  els["lib-nomtl"].addEventListener("change", renderLibrary);
  var tagTimer = null;
  els["lib-tag"].addEventListener("input", function () {
    clearTimeout(tagTimer);
    tagTimer = setTimeout(renderLibrary, 150);
  });
  els["lib-clear"].addEventListener("click", function () {
    els["lib-kind"].value = ""; els["lib-tag"].value = ""; els["lib-source"].value = "";
    els["lib-lang"].value = ""; els["lib-state"].value = ""; els["lib-progress"].value = ""; els["lib-favonly"].checked = false;
    els["lib-len"].value = ""; els["lib-len-min"].value = ""; els["lib-len-max"].value = "";
    els["lib-len-custom"].classList.add("hidden"); els["lib-nobl"].checked = false; els["lib-dupes"].checked = false; els["lib-shelf"].value = ""; els["lib-nomtl"].checked = false; lib.cat = "";
    renderCategories(); renderLibrary();
  });

  // "Check updates": re-fetch every novel's info from its source, one at a time
  // (sequential on purpose — polite to the sources), and badge the ones that
  // gained chapters. Click again to stop mid-run.
  var checking = false;
  // Re-read each novel's details from its source, one at a time. Used by
  // "Check updates" (every novel) and "Fill in details" (those missing some).
  function refreshNovels(items, btn, idleText, doneText) {
    if (checking) { checking = false; return; }
    if (!items.length) return;
    checking = true;
    var other = btn === els["lib-update"] ? els["lib-fill"] : els["lib-update"];
    other.disabled = true;
    btn.textContent = "■ Stop";
    var updates = loadUpdates();
    var i = 0, found = 0;
    function finish() {
      checking = false;
      other.disabled = false;
      btn.textContent = idleText;
      lib.hay = {};
      renderFilterOptions();
      renderCategories();
      renderLibrary();
      els["lib-status"].textContent = doneText(i, found);
    }
    function next() {
      if (!checking || i >= items.length) { finish(); return; }
      var n = items[i++];
      els["lib-status"].textContent = "Refreshing " + i + "/" + items.length + ": " + (n.title || "");
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
  }
  els["lib-update"].addEventListener("click", function () {
    refreshNovels(lib.all.slice(), this, "⟳ Check updates", function (done, found) {
      return found ? "Update check done — " + found + " novel(s) have new chapters."
        : "Update check done — no new chapters found.";
    });
  });

  // Novels the filters can't place: no status, no genres or no description,
  // usually because they were added before those were recorded.
  function missingDetails(n) { return !statusOf(n) || !(n.tags && n.tags.length) || !n.synopsis; }
  function updateFillButton() {
    if (checking) return;
    var n = lib.all.filter(missingDetails).length;
    els["lib-fill"].classList.toggle("hidden", !n);
    els["lib-fill"].textContent = "✚ Fill in details (" + n + ")";
  }
  els["lib-fill"].addEventListener("click", function () {
    var items = lib.all.filter(missingDetails);
    refreshNovels(items, this, "✚ Fill in details", function (done) {
      var left = lib.all.filter(missingDetails).length;
      return "Re-read the details of " + done + " novel(s)." +
        (left ? " " + left + " still lack some — their sources don't provide them." : "");
    });
  });

  // ---- Saved views ----
  function renderViews(selected) {
    var views = loadViews();
    var sel = els["lib-views"];
    sel.innerHTML = "";
    var head = document.createElement("option"); head.value = "";
    head.textContent = Object.keys(views).length ? "Saved views" : "No saved views";
    sel.appendChild(head);
    Object.keys(views).sort().forEach(function (name) {
      var o = document.createElement("option"); o.value = name; o.textContent = name; sel.appendChild(o);
    });
    sel.value = selected && views[selected] ? selected : "";
    els["lib-view-del"].classList.toggle("hidden", !sel.value);
  }
  els["lib-views"].addEventListener("change", function () {
    var views = loadViews();
    if (this.value && views[this.value]) {
      applyView(views[this.value]);
      renderCategories();
      renderLibrary();
    }
    els["lib-view-del"].classList.toggle("hidden", !this.value);
  });
  els["lib-view-save"].addEventListener("click", function () {
    var name = (prompt("Name this view (for example “Completed manhwa”):", els["lib-views"].value || "") || "").trim();
    if (!name) return;
    var views = loadViews();
    views[name] = currentView();
    saveViews(views);
    renderViews(name);
  });
  els["lib-view-del"].addEventListener("click", function () {
    var name = els["lib-views"].value;
    if (!name || !confirm("Delete the saved view “" + name + "”?")) return;
    var views = loadViews();
    delete views[name];
    saveViews(views);
    renderViews("");
  });

  // ---- Search the sources ----
  // When the library doesn't have it, look for it where it can be downloaded.
  // Results only list what each source returns; nothing is added until picked.
  var web = { job: null, timer: null, query: "" };
  var JOB_STATES = ["PENDING", "RUNNING", "SUCCESS", "FAILED", "CANCELED", "PAUSED"];
  function statusName(s) { return typeof s === "number" ? (JOB_STATES[s] || String(s)) : s; }
  function updateWebButton() {
    var q = els["search"].value.trim();
    var btn = els["lib-web-btn"];
    btn.classList.toggle("hidden", q.length < 2);
    btn.textContent = "🔎 Search the sources for “" + q + "”";
  }
  function stopWebSearch() {
    clearTimeout(web.timer);
    if (web.job) api("/job/" + web.job + "/cancel", { method: "POST" }).catch(function () {});
    web.job = null;
    els["lib-web-stop"].classList.add("hidden");
  }
  els["lib-web-stop"].addEventListener("click", function () {
    stopWebSearch();
    els["lib-web-status"].textContent = "· stopped";
  });
  els["lib-web-btn"].addEventListener("click", function () {
    var q = els["search"].value.trim();
    if (q.length < 2) return;
    stopWebSearch();
    web.query = q;
    els["lib-web"].classList.remove("hidden");
    els["lib-web-list"].innerHTML = "";
    els["lib-web-status"].textContent = "· searching…";
    api("/job/create/search-sources", {
      method: "POST",
      body: JSON.stringify({ query: q.slice(0, 50), results_only: true })
    }).then(function (job) {
      web.job = job.id;
      els["lib-web-stop"].classList.remove("hidden");
      pollWeb();
    }).catch(function (e) { els["lib-web-status"].textContent = "· couldn't search: " + e.message; });
  });
  function pollWeb() {
    var id = web.job;
    if (!id) return;
    api("/job/" + id).then(function (job) {
      if (web.job !== id) return;
      renderWebResults((job.extra && job.extra.search_results) || []);
      var running = statusName(job.status) === "PENDING" || statusName(job.status) === "RUNNING";
      var n = els["lib-web-list"].children.length;
      els["lib-web-status"].textContent = "· " + n + " found" +
        (running ? " — searching " + (job.done || 0) + "/" + (job.total || "?") + " sources…" : "");
      if (running) web.timer = setTimeout(pollWeb, 2000);
      else { web.job = null; els["lib-web-stop"].classList.add("hidden"); }
    }).catch(function () { web.timer = setTimeout(pollWeb, 4000); });
  }
  function renderWebResults(results) {
    var have = {};
    (lib.everything || lib.all).forEach(function (n) { have[n.url] = n; });
    var box = els["lib-web-list"];
    box.innerHTML = "";
    results.slice(0, 60).forEach(function (r) {
      var row = document.createElement("div"); row.className = "web-row";
      var t = document.createElement("div"); t.className = "t";
      var b = document.createElement("b"); b.textContent = r.title || r.url;
      var m = document.createElement("span"); m.className = "muted";
      var host = ""; try { host = new URL(r.url).hostname.replace(/^www\\./, ""); } catch (e) { host = ""; }
      m.textContent = [host, r.info || ""].filter(Boolean).join(" · ");
      t.appendChild(b); t.appendChild(m); row.appendChild(t);
      var owned = have[r.url];
      if (owned) {
        var open = document.createElement("button"); open.className = "secondary"; open.textContent = "In library — open";
        open.addEventListener("click", function () { openNovel(owned); });
        row.appendChild(open);
      } else {
        var add = document.createElement("button"); add.textContent = "Add";
        add.title = "Add its details to your library";
        var full = document.createElement("button"); full.className = "secondary"; full.textContent = "Add & download";
        full.title = "Add it and download every chapter";
        add.addEventListener("click", function () { addFromWeb(r.url, false, row); });
        full.addEventListener("click", function () { addFromWeb(r.url, true, row); });
        row.appendChild(add); row.appendChild(full);
      }
      box.appendChild(row);
    });
  }
  function addFromWeb(url, full, row) {
    row.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
    var note = document.createElement("span"); note.className = "muted"; note.textContent = "adding…";
    row.appendChild(note);
    api("/job/create/fetch-novels", { method: "POST", body: JSON.stringify({ urls: [url], full: full }) })
      .then(function (job) {
        (function wait() {
          api("/job/" + job.id).then(function (j) {
            var st = statusName(j.status);
            if (st === "PENDING" || st === "RUNNING") {
              note.textContent = full ? "downloading… " + (j.done || 0) + "/" + (j.total || "?") : "adding…";
              setTimeout(wait, 2500);
              return;
            }
            note.textContent = st === "SUCCESS" ? "✓ added" : "failed" + (j.error ? ": " + String(j.error).split("\\n").pop() : "");
            if (st === "SUCCESS") loadLibrary();
          }).catch(function () { setTimeout(wait, 5000); });
        })();
      })
      .catch(function (e) { note.textContent = "couldn't add: " + e.message; });
  }

  // ---- New chapters ----
  // A novel has new chapters when one was added after you last read it.
  function hasNewChapters(n) {
    var pe = posOf(n.id);
    var newest = (lib.latest || {})[n.id];
    return !!(pe && pe.ts && newest && newest > pe.ts);
  }
  function watched(n, scope) {
    if (scope === "all") return true;
    var fav = isFav(n.id), reading = !!posOf(n.id);
    if (scope === "favorites") return fav;
    if (scope === "reading") return reading;
    return fav || reading;
  }
  var NOTIFIED = "lncrawl_reader_notified";  // { novelId: newest chapter ts told about }
  function renderAuto() {
    var a = loadAuto();
    els["auto-on"].checked = !!a.enabled;
    els["auto-scope"].value = a.scope || "both";
    els["auto-dl"].checked = !!a.download;
    els["auto-notify"].checked = !!a.notify;
    var fresh = lib.all.filter(function (n) { return hasNewChapters(n) && watched(n, a.scope || "both"); });
    els["lib-auto-btn"].textContent = "🔔 New chapters" + (fresh.length ? " (" + fresh.length + ")" : "");
    els["auto-note"].textContent = (a.enabled
      ? "The server checks these once a day" + (a.download ? " and downloads what it finds." : ".")
      : "Off: use ⟳ Check updates to look now.") +
      (fresh.length ? " " + fresh.length + " novel(s) have chapters you haven't read." : "");
    return fresh;
  }
  function notifyNew() {
    var a = loadAuto();
    if (!a.notify || !("Notification" in window) || Notification.permission !== "granted") return;
    var told; try { told = JSON.parse(localStorage.getItem(NOTIFIED) || "{}"); } catch (e) { told = {}; }
    var fresh = renderAuto().filter(function (n) { return (told[n.id] || 0) < (lib.latest[n.id] || 0); });
    if (!fresh.length) return;
    fresh.forEach(function (n) { told[n.id] = lib.latest[n.id]; });
    localStorage.setItem(NOTIFIED, JSON.stringify(told));
    var names = fresh.slice(0, 3).map(function (n) { return n.title; }).join(", ");
    try {
      new Notification("New chapters", {
        body: names + (fresh.length > 3 ? " and " + (fresh.length - 3) + " more" : ""),
        tag: "lncrawl-new-chapters"
      });
    } catch (e) { /* notifications blocked */ }
  }
  function saveAutoFromForm() {
    saveAuto({
      enabled: els["auto-on"].checked, scope: els["auto-scope"].value,
      download: els["auto-dl"].checked, notify: els["auto-notify"].checked
    });
    renderAuto();
  }
  els["lib-auto-btn"].addEventListener("click", function () {
    els["lib-auto"].classList.toggle("hidden");
    renderAuto();
  });
  ["auto-on", "auto-scope", "auto-dl"].forEach(function (k) { els[k].addEventListener("change", saveAutoFromForm); });
  els["auto-notify"].addEventListener("change", function () {
    var box = this;
    if (box.checked && "Notification" in window && Notification.permission !== "granted") {
      Notification.requestPermission().then(function (p) {
        if (p !== "granted") { box.checked = false; alert("Notifications are blocked for this site in your browser settings."); }
        saveAutoFromForm(); notifyNew();
      });
      return;
    }
    if (box.checked && !("Notification" in window)) { box.checked = false; alert("This browser can't show notifications."); }
    saveAutoFromForm();
  });

  // ---- Shelves (your own lists, kept on the server as libraries) ----
  function loadShelves() {
    return Promise.all([
      api("/library/my/list").catch(function () { return []; }),
      api("/library/my/membership").catch(function () { return {}; })
    ]).then(function (r) {
      lib.shelves = r[0] || [];
      lib.membership = r[1] || {};
      renderShelfFilter();
      if (current.novel) renderShelfBox(current.novel);
    });
  }
  function shelvesOf(novelId) {
    return (lib.shelves || []).filter(function (s) { return ((lib.membership || {})[s.id] || []).indexOf(novelId) >= 0; });
  }
  function renderShelfFilter() {
    var sel = els["lib-shelf"];
    var keep = lib.pendingShelf || sel.value;
    lib.pendingShelf = null;
    sel.innerHTML = "";
    var all = document.createElement("option"); all.value = ""; all.textContent = "All shelves"; sel.appendChild(all);
    (lib.shelves || []).forEach(function (s) {
      var o = document.createElement("option"); o.value = s.id;
      o.textContent = s.name + " (" + (((lib.membership || {})[s.id]) || []).length + ")";
      sel.appendChild(o);
    });
    sel.value = (lib.shelves || []).some(function (s) { return s.id === keep; }) ? keep : "";
    sel.classList.toggle("hidden", !(lib.shelves || []).length);
  }
  function renderShelfBox(novel) {
    var box = els["shelf-box"];
    box.innerHTML = "";
    (lib.shelves || []).forEach(function (s) {
      var on = ((lib.membership || {})[s.id] || []).indexOf(novel.id) >= 0;
      var chip = document.createElement("span"); chip.className = "chip" + (on ? " active" : "");
      chip.textContent = (on ? "✓ " : "") + s.name;
      chip.addEventListener("click", function () {
        var path = on ? "/library/" + s.id + "/novels/" + novel.id : "/library/" + s.id + "/novel/" + novel.id;
        api(path, { method: on ? "DELETE" : "PUT" }).then(loadShelves)
          .catch(function (e) { alert("Couldn't change the shelf: " + e.message); });
      });
      box.appendChild(chip);
    });
    var add = document.createElement("span"); add.className = "chip"; add.textContent = "+ New shelf";
    add.addEventListener("click", function () {
      var name = (prompt("Name the new shelf (for example “Plan to read”):") || "").trim();
      if (!name) return;
      api("/library", { method: "POST", body: JSON.stringify({ name: name }) })
        .then(function (shelf) { return api("/library/" + shelf.id + "/novel/" + novel.id, { method: "PUT" }); })
        .then(loadShelves)
        .catch(function (e) { alert("Couldn't make the shelf: " + e.message); });
    });
    box.appendChild(add);
    var names = shelvesOf(novel.id).map(function (s) { return s.name; });
    els["shelf-btn"].textContent = "📚 " + (names.length ? names.join(", ") : "Shelves");
  }
  els["shelf-btn"].addEventListener("click", function () {
    els["shelf-box"].classList.toggle("hidden");
    if (current.novel) renderShelfBox(current.novel);
  });

  // ---- Your rating ----
  function renderRating(novel) {
    var mine = loadRatings()[novel.id] || 0;
    var box = els["rate-box"];
    box.innerHTML = "";
    for (var i = 1; i <= 5; i++) {
      (function (v) {
        var st = document.createElement("span"); st.textContent = "★";
        st.className = v <= mine ? "on" : "";
        st.title = v + " star" + (v > 1 ? "s" : "") + (v === mine ? " — click again to clear" : "");
        st.addEventListener("click", function () {
          var r = loadRatings();
          if (r[novel.id] === v) delete r[novel.id]; else r[novel.id] = v;
          saveRatings(r);
          renderRating(novel);
        });
        box.appendChild(st);
      })(i);
    }
  }

  // ---- Progress by hand (for reading done elsewhere) ----
  // The chapter at a position, found by its place in the list.
  function chapterAt(novel, serial) {
    var n = Math.max(1, Math.min(serial, novel.chapter_count || serial));
    return api("/novel/" + novel.id + "/chapters?limit=1&offset=" + (n - 1)).then(function (res) {
      return ((res && res.items) || [])[0] || null;
    });
  }
  function setProgress(novel, serial, scroll) {
    return chapterAt(novel, serial).then(function (ch) {
      if (!ch) throw new Error("no such chapter");
      var p = loadPos();
      p[novel.id] = { id: ch.id, serial: ch.serial, ts: Date.now(), scroll: scroll };
      localStorage.setItem(POS, JSON.stringify(p));
      schedulePush();
      openNovel(novel);
    });
  }
  els["prog-done"].addEventListener("click", function () {
    if (!current.novel) return;
    setProgress(current.novel, current.novel.chapter_count || 1, 1)
      .catch(function (e) { alert("Couldn't mark it finished: " + e.message); });
  });
  els["prog-unread"].addEventListener("click", function () {
    if (!current.novel || !confirm("Forget your place in “" + current.novel.title + "”?")) return;
    var p = loadPos(); delete p[current.novel.id];
    localStorage.setItem(POS, JSON.stringify(p));
    schedulePush();
    openNovel(current.novel);
  });
  els["prog-set"].addEventListener("click", function () {
    if (!current.novel) return;
    var pe = posOf(current.novel.id);
    var raw = prompt("Which chapter are you on? (1–" + (current.novel.chapter_count || "?") + ")", pe && pe.serial ? pe.serial : "");
    var n = parseInt(raw || "", 10);
    if (!n || n < 1) return;
    setProgress(current.novel, n, 0).catch(function (e) { alert("Couldn't set it: " + e.message); });
  });

  // ---- Novel ----
  var current = { novel: null, chapters: [], offset: 0 };
  var repairJobs = {};
  var repairedArtifacts = {};
  function refreshCompleteness(novelId) {
    return api("/novel/" + novelId + "/completeness").then(function (report) {
      if (!current.novel || current.novel.id !== novelId) return;
      els["novel-completeness"].textContent =
        (report.complete ? "Complete" : "Incomplete") + " · " + report.downloaded + "/" + report.total +
        " downloaded · " + report.missing + " missing · " + report.failed + " failed · " + report.empty + " empty";
      els["novel-completeness"].title = (report.gaps || []).slice(0, 20).map(function (gap) {
        return "#" + gap.serial + " " + gap.title + ": " + gap.status + (gap.error ? " — " + gap.error : "");
      }).join("\\n");
    }).catch(function (error) {
      if (current.novel && current.novel.id === novelId) {
        els["novel-completeness"].textContent = "Couldn't check completeness: " + error.message;
      }
    });
  }
  function pollRepair(novelId, jobId) {
    api("/job/" + jobId).then(function (job) {
      var active = job.status === 0 || job.status === 1 || job.status === 5 ||
        job.status === "PENDING" || job.status === "RUNNING" || job.status === "PAUSED";
      var visible = current.novel && current.novel.id === novelId;
      if (active) {
        if (visible) els["repair-status"].textContent = "Repairing chapters and rebuilding EPUB… " + job.done + "/" + job.total;
        setTimeout(function () { pollRepair(novelId, jobId); }, 3000);
        return;
      }
      delete repairJobs[novelId];
      var extra = job.extra || {};
      var success = job.status === 2 || job.status === "SUCCESS";
      if (success && extra.artifact_id) repairedArtifacts[novelId] = extra.artifact_id;
      if (!visible) return;
      els["repair-btn"].disabled = false;
      els["repair-status"].textContent = success && extra.artifact_id
        ? "Repair complete. The rebuilt EPUB is ready to download."
        : "Repair did not finish: " + (job.error || "Some downloads failed. Retry to continue.");
      els["repair-download"].classList.toggle("hidden", !repairedArtifacts[novelId]);
      refreshCompleteness(novelId);
      current.chapters = []; current.offset = 0; els["chap-list"].innerHTML = "";
      loadChapters();
    }).catch(function () {
      setTimeout(function () { pollRepair(novelId, jobId); }, 5000);
    });
  }
  els["repair-btn"].addEventListener("click", function () {
    if (!current.novel) return;
    var novelId = current.novel.id;
    this.disabled = true;
    els["repair-status"].textContent = "Queuing missing-chapter repair…";
    delete repairedArtifacts[novelId];
    els["repair-download"].classList.add("hidden");
    api("/novel/" + novelId + "/repair", { method: "POST" }).then(function (job) {
      repairJobs[novelId] = job.id;
      pollRepair(novelId, job.id);
    }).catch(function (error) {
      if (!current.novel || current.novel.id !== novelId) return;
      els["repair-btn"].disabled = false;
      els["repair-status"].textContent = "Couldn't start repair: " + error.message;
    });
  });
  els["repair-download"].addEventListener("click", function () {
    if (!current.novel) return;
    var novelId = current.novel.id;
    var artifactId = repairedArtifacts[novelId];
    if (!artifactId) return;
    var btn = this; btn.disabled = true;
    api("/artifact/" + artifactId).then(function (artifact) {
      return fetch("/static/" + artifact.output_file.split("/").map(encodeURIComponent).join("/") +
        "?token=" + encodeURIComponent(token()), {
        headers: { "Authorization": "Bearer " + token() }
      }).then(function (response) {
        if (!response.ok) throw new Error("Download failed: HTTP " + response.status);
        return response.blob();
      }).then(function (blob) {
        var url = URL.createObjectURL(blob);
        var link = document.createElement("a");
        link.href = url; link.download = artifact.file_name;
        document.body.appendChild(link); link.click(); link.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
      });
    }).catch(function (error) {
      if (current.novel && current.novel.id === novelId) els["repair-status"].textContent = error.message;
    }).finally(function () { btn.disabled = false; });
  });
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
    els["novel-completeness"].textContent = "Checking chapter completeness…";
    els["repair-btn"].disabled = !!repairJobs[novel.id];
    els["repair-status"].textContent = repairJobs[novel.id] ? "Repair in progress…" : "";
    els["repair-download"].classList.toggle("hidden", !repairedArtifacts[novel.id]);
    refreshCompleteness(novel.id);
    renderNovelHeader(novel);
    maybeRefreshInfo(novel);

    // opening the novel acknowledges its "+N new" badge
    var updates = loadUpdates();
    if (updates[novel.id]) { delete updates[novel.id]; saveUpdates(updates); }

    renderFavBtn(novel);
    renderBookmarks(novel);
    renderRating(novel);
    els["shelf-box"].classList.add("hidden");
    renderShelfBox(novel);

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
    els["deep-btn"].classList.add("hidden");
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
      if (gaps) {
        els["heal-btn"].classList.remove("hidden");
        els["deep-btn"].classList.remove("hidden");
        maybeAutoHeal();
      }
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

  // Deep heal: a background job searches other sites for the missing chapters,
  // downloads them, and copies matches in. Poll it and refresh when done.
  els["deep-btn"].addEventListener("click", function () {
    if (!current.novel) return;
    var btn = this; btn.disabled = true;
    var novelId = current.novel.id;
    els["heal-msg"].textContent = "Searching other sites for the missing chapters…";
    els["heal-msg"].classList.remove("hidden");
    api("/novel/" + novelId + "/heal-deep", { method: "POST" }).then(function (job) {
      (function poll() {
        api("/job/" + job.id).then(function (j) {
          if (!current.novel || current.novel.id !== novelId) { btn.disabled = false; return; }
          var ex = j.extra || {};
          var active = j.status === 0 || j.status === 1 || j.status === "PENDING" || j.status === "RUNNING";
          if (active) {
            if (ex.source) {
              els["heal-msg"].textContent = (ex.phase === "searching" ? "Searching " : "Downloading from ") +
                ex.source + "… (" + (j.done || 0) + "/" + (j.total || 0) + " filled)";
            }
            setTimeout(poll, 3000);
            return;
          }
          btn.disabled = false;
          els["heal-msg"].textContent = ex.message || (j.error ? ("Failed: " + j.error) : "Done.");
          if (ex.healed) {
            current.chapters = []; current.offset = 0; els["chap-list"].innerHTML = "";
            loadChapters();
          }
        }).catch(function () { setTimeout(poll, 5000); });
      })();
    }).catch(function (e) {
      btn.disabled = false;
      els["heal-msg"].textContent = "Couldn't start the search: " + e.message;
    });
  });

  els["merge-btn"].addEventListener("click", function () {
    if (!current.novel) return;
    if (!confirm('Combine all copies of this title into the most complete one and delete the others? Chapters that only exist in a deleted copy under a different chapter name can be lost.')) return;
    var btn = this; btn.disabled = true;
    api("/novel/" + current.novel.id + "/merge", { method: "POST" }).then(function (res) {
      var cid = res && res.canonical_id;
      return fetchAllNovels("").then(function (items) {
        lib.all = items;
        renderFilterOptions();
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
      // a short text chapter that fits on screen never scrolls, so count it
      // read in full; manga pages load later and always scroll
      if (!imgCount) {
        setTimeout(function () {
          if (current.chapterId !== chapterId) return;
          if (document.documentElement.scrollHeight - window.innerHeight <= 0) {
            saveScroll(novel.id, chapterId, 1);
          }
        }, 800);
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
  function toggleFav(id) {
    var favs = loadFavs();
    var i = favs.indexOf(id);
    if (i >= 0) favs.splice(i, 1); else favs.push(id);
    saveFavs(favs);
  }
  els["fav-btn"].addEventListener("click", function () {
    if (!current.novel) return;
    toggleFav(current.novel.id);
    renderFavBtn(current.novel);
  });

  // ---- Bookmarks ----
  // Every bookmark across the library, newest first, so a saved spot can be
  // found without remembering which novel it was in.
  function renderAllBookmarks() {
    var m = loadBmks();
    var byId = {}; lib.all.forEach(function (n) { byId[n.id] = n; });
    var rows = [];
    Object.keys(m).forEach(function (novelId) {
      (m[novelId] || []).forEach(function (b, idx) { rows.push({ novelId: novelId, b: b, idx: idx }); });
    });
    rows.sort(function (x, y) { return (y.b.ts || 0) - (x.b.ts || 0); });
    els["lib-bmks-btn"].textContent = "🔖 Bookmarks" + (rows.length ? " (" + rows.length + ")" : "");
    els["lib-bmks-count"].textContent = rows.length ? rows.length + " saved" : "";
    var box = els["lib-bmks-list"];
    box.innerHTML = "";
    if (!rows.length) {
      box.innerHTML = "<p class='muted'>No bookmarks yet. While reading, press 🔖 to save your spot.</p>";
      return;
    }
    rows.forEach(function (r) {
      var novel = byId[r.novelId];
      var row = document.createElement("div"); row.className = "bmk-row";
      var icon = document.createElement("span"); icon.textContent = "🔖";
      var t = document.createElement("span"); t.className = "t";
      var nv = document.createElement("span"); nv.className = "nv";
      nv.textContent = novel ? (novel.title || "Untitled") : "(novel no longer in library)";
      t.appendChild(nv);
      t.appendChild(document.createTextNode("  ·  " + (r.b.title || ("Chapter " + (r.b.serial || ""))) +
        " · " + Math.round((r.b.scroll || 0) * 100) + "%"));
      var when = document.createElement("span"); when.className = "when";
      when.textContent = r.b.ts ? new Date(r.b.ts).toLocaleDateString() : "";
      var x = document.createElement("span"); x.className = "x"; x.textContent = "✕"; x.title = "Remove bookmark";
      x.addEventListener("click", function (e) {
        e.stopPropagation();
        var mm = loadBmks(); var list = mm[r.novelId] || [];
        // find it again by value: indexes shift as others are removed
        var i = list.findIndex(function (b) { return b.id === r.b.id && b.ts === r.b.ts; });
        if (i >= 0) list.splice(i, 1);
        if (!list.length) delete mm[r.novelId];
        saveBmks(mm);
        renderAllBookmarks();
      });
      row.appendChild(icon); row.appendChild(t); row.appendChild(when); row.appendChild(x);
      if (novel) {
        row.addEventListener("click", function () {
          current = { novel: novel, chapters: [], offset: 0 };
          openChapter(r.b.id, r.b.scroll || 0);
        });
      } else {
        row.style.cursor = "default";
      }
      box.appendChild(row);
    });
  }
  els["lib-bmks-btn"].addEventListener("click", function () {
    var open = els["lib-bmks"].classList.toggle("hidden") === false;
    if (open) renderAllBookmarks();
  });

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
    searchTimer = setTimeout(renderLibrary, 150);
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
    applyView(loadView());
    renderViews();
    if (!token()) { show("auth"); return; }
    api("/auth/me", { method: "GET" })
      .then(function () { show("view-library"); loadLibrary(); syncState(); })
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
