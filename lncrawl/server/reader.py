"""Standalone in-app Reader page.

A self-contained, dependency-free reading experience served at ``GET /reader``.
It browses the downloaded library, lists a novel's chapters, and renders chapter
content (text and manga images) with next/previous navigation, adjustable font
size, and per-novel reading-position memory. Kept outside ``lncrawl/server/web``
(overwritten by the web-sync workflow) so it survives front-end rebuilds; the
markup is embedded as a string so it always ships with the wheel/exe.
"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .caching import blob_response, text_blob

router = APIRouter()

# NOTE: plain string (not an f-string) on purpose — the embedded CSS/JS is full
# of ``{}`` braces. Avoid backslash escapes inside the JS strings.
_READER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta name="theme-color" content="#16141d" />
<meta name="mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
<meta name="apple-mobile-web-app-title" content="LNCrawl" />
<link rel="manifest" href="/reader/manifest.webmanifest" />
<link rel="apple-touch-icon" href="/reader/icon.svg" />
<title>LNCrawl · Reader</title>
<style>
  /* A reading room at night: ink walls, paper text, one lamp. The lamp colour
     marks the thing to press next and where you are; nothing else uses it.
     Titles and the chapter text are set in a book face; the controls are not.
     The themes (paper, sepia) only swap these colours. */
  :root {
    color-scheme: dark;
    --bg: #16141d; --bar: rgba(22, 20, 29, .86); --panel: #1f1c29; --raised: #2b2738;
    --line: #34304a; --text: #efe9dc; --dim: #a59fb3; --faint: #7d778c;
    --lamp: #f2b84b; --lamp-ink: #201803; --link: #9cc8ff;
    --good: #7fd1a5; --warn: #f0b46a; --bad: #ff8f84; --shade: rgba(0, 0, 0, .45);
    --serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
    --sans: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, Roboto, sans-serif;
  }
  body.t-light {
    color-scheme: light;
    --bg: #f7f3ea; --bar: rgba(247, 243, 234, .9); --panel: #fffdf8; --raised: #ece5d6;
    --line: #ddd3c0; --text: #2a2530; --dim: #5f5868; --faint: #8a8392;
    --lamp: #b7791f; --lamp-ink: #fffaf0; --link: #2f5f9e;
    --good: #2f7a52; --warn: #9a5a12; --bad: #b4382c; --shade: rgba(60, 40, 10, .18);
  }
  body.t-sepia {
    color-scheme: light;
    --bg: #f4ecd8; --bar: rgba(244, 236, 216, .9); --panel: #f9f2e0; --raised: #e6d9ba;
    --line: #d8c8a4; --text: #4b3b2a; --dim: #7a6650; --faint: #9a876d;
    --lamp: #a8641c; --lamp-ink: #fff8ea; --link: #7a4b1e;
    --good: #4f6b2c; --warn: #8a5a14; --bad: #9c3b2b; --shade: rgba(80, 55, 20, .2);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0; font-family: var(--sans); font-size: 15px; line-height: 1.55;
    background: var(--bg); color: var(--text);
  }
  :focus-visible { outline: 2px solid var(--lamp); outline-offset: 2px; }
  a { color: var(--link); }

  /* the bar along the top */
  .topbar {
    position: sticky; top: 0; z-index: 10; display: flex; gap: 14px; align-items: center;
    padding: 10px 22px; background: var(--bar); border-bottom: 1px solid var(--line);
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  }
  .topbar h1 {
    font-family: var(--serif); font-weight: 500; font-size: 19px; margin: 0; flex: 0 0 auto;
    letter-spacing: .01em; color: var(--text);
  }
  .topbar h1 small { font-family: var(--sans); font-size: 12.5px; color: var(--faint); margin-left: 6px; }
  .topbar .spacer { flex: 1; }
  .topbar .back { color: var(--dim); text-decoration: none; }
  .topbar .back:hover { color: var(--text); text-decoration: none; }

  input[type=text], input[type=number] {
    padding: 9px 12px; border-radius: 9px; border: 1px solid var(--line);
    background: var(--panel); color: var(--text); font: inherit; font-size: 14px; width: 100%;
  }
  input::placeholder { color: var(--faint); }
  #search { border-radius: 999px; padding-left: 16px; }
  button {
    padding: 8px 14px; border: 1px solid transparent; border-radius: 9px; background: var(--raised);
    color: var(--text); font: inherit; font-size: 14px; cursor: pointer;
  }
  button:hover { border-color: var(--line); filter: brightness(1.08); }
  button.primary { background: var(--lamp); color: var(--lamp-ink); font-weight: 650; }
  button.primary:hover { filter: brightness(1.06); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  select {
    padding: 8px 11px; border-radius: 9px; border: 1px solid var(--line);
    background: var(--panel); color: var(--text); font: inherit; font-size: 13.5px;
  }
  label { display: block; font-size: 13px; color: var(--dim); margin: 10px 0 4px; }
  input[type=checkbox] { accent-color: var(--lamp); }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 22px 22px 96px; }
  .muted { color: var(--dim); }
  .hidden { display: none !important; }

  /* panels: bookmarks, stats, shelves, new chapters */
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 14px 16px; margin-bottom: 12px; cursor: pointer;
  }
  .card h3 { margin: 0 0 3px; font-size: 16px; }
  .card .meta { color: var(--dim); font-size: 13px; }
  .shelf-title { font-family: var(--serif); font-size: 19px; color: var(--text); margin: 4px 0 12px; }
  .shelf-title .see-all { float: right; font-family: var(--sans); font-size: 13px; color: var(--link); cursor: pointer; }

  /* the library: covers on a shelf, or a list */
  .toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
  .toolbar.filters {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 12px;
  }
  .toolbar.filters input[type=text] { flex: 1 1 220px; max-width: 360px; margin: 0; }
  .toolbar .check { display: flex; align-items: center; gap: 6px; font-size: 13.5px; margin: 0; cursor: pointer; color: var(--text); }
  .chap-range { display: inline-flex; align-items: center; gap: 5px; font-size: 13px; }
  .chap-range input[type=number] { width: 70px; margin: 0; padding: 7px 8px; }
  #lib-filters-btn.on { background: var(--lamp); color: var(--lamp-ink); }
  #lib-layout { min-width: 92px; }
  #lib-status { font-size: 13.5px; margin: 6px 0 14px; }
  .tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
  #lib-cats { flex: 1 1 100%; margin-top: 4px; }
  .chip {
    font-size: 12px; color: var(--dim); background: transparent; border: 1px solid var(--line);
    border-radius: 999px; padding: 3px 10px; cursor: pointer; white-space: nowrap;
  }
  .chip:hover { color: var(--text); border-color: var(--faint); }
  .chip.active { background: var(--lamp); color: var(--lamp-ink); border-color: var(--lamp); }

  .libcard { display: flex; gap: 16px; align-items: flex-start; position: relative; }
  .libcard .cover, #novel-cover, .shelf .mini img {
    background: linear-gradient(160deg, var(--raised), var(--panel)); border: 1px solid var(--line);
    object-fit: cover; display: block;
  }
  .libcard .cover { width: 72px; height: 108px; flex: 0 0 72px; border-radius: 6px; }
  .libcard .body { flex: 1; min-width: 0; }
  .libcard h3 { font-family: var(--serif); font-weight: 500; font-size: 18px; line-height: 1.3; }
  .libcard .meta.resume { color: var(--lamp); }
  .libcard .desc {
    color: var(--dim); font-size: 13.5px; margin: 6px 0 0; max-width: 72ch;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .libcard .desc.open { display: block; -webkit-line-clamp: unset; }
  .libcard .desc-more { color: var(--link); font-size: 12.5px; cursor: pointer; }
  .libcard .fav-star {
    cursor: pointer; color: var(--faint); font-size: 18px; line-height: 1; float: right; padding: 0 2px;
    font-family: var(--sans);
  }
  .libcard .fav-star.on { color: var(--lamp); }
  #lib-list:not(.grid) .libcard { padding: 14px; }
  #lib-list:not(.grid) .libcard:hover { border-color: var(--faint); }

  #lib-list.grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(158px, 1fr)); gap: 26px 20px;
  }
  #lib-list.grid .libcard {
    flex-direction: column; gap: 0; background: transparent; border: 0; padding: 0; margin: 0;
  }
  #lib-list.grid .libcard .cover {
    width: 100%; height: auto; flex: none; aspect-ratio: 2 / 3; border-radius: 8px;
    box-shadow: 0 10px 24px -12px var(--shade); transition: transform .18s ease, box-shadow .18s ease;
  }
  #lib-list.grid .libcard:hover .cover { transform: translateY(-3px); box-shadow: 0 16px 28px -14px var(--shade); }
  #lib-list.grid .libcard .body { width: 100%; padding-top: 10px; }
  #lib-list.grid .libcard h3 { font-size: 15.5px; }
  #lib-list.grid .libcard .meta { font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #lib-list.grid .libcard .desc, #lib-list.grid .libcard .desc-more, #lib-list.grid .libcard .tags { display: none; }
  #lib-list.grid .libcard .fav-star {
    position: absolute; top: 8px; right: 8px; float: none; width: 30px; height: 30px; padding: 0;
    display: flex; align-items: center; justify-content: center; border-radius: 999px;
    background: rgba(15, 12, 20, .62); color: #efe9dc; font-size: 16px;
  }
  #lib-list.grid .libcard .fav-star.on { color: #f2b84b; }
  #lib-list.grid .lib-more { grid-column: 1 / -1; }
  #lib-list.grid .libcard .progress { margin-top: 8px; }

  .badge {
    display: inline-block; font-family: var(--sans); font-size: 10.5px; font-weight: 600; letter-spacing: .02em;
    color: var(--dim); background: transparent; border: 1px solid var(--line);
    border-radius: 999px; padding: 0 7px; margin-left: 6px; vertical-align: 2px; line-height: 17px;
  }
  .badge.new, .badge.fresh { color: var(--good); border-color: var(--good); }
  .badge.nochap { color: var(--warn); border-color: var(--warn); }
  .badge.part { color: var(--link); border-color: var(--link); }
  .badge.away { color: var(--dim); border-color: var(--dim); }
  .bar { height: 10px; border-radius: 5px; background: var(--line); overflow: hidden; margin: 10px 0 6px; }
  .bar > div { height: 100%; width: 0; background: var(--lamp); transition: width .3s; }
  #unf-kinds label { display: block; margin: 6px 0; }
  .libcard.away { opacity: .55; }
  .progress { height: 3px; background: var(--raised); border-radius: 999px; margin-top: 6px; overflow: hidden; }
  .progress > span { display: block; height: 100%; background: var(--lamp); }

  /* continue reading */
  .shelf { display: flex; gap: 16px; overflow-x: auto; padding-bottom: 10px; margin-bottom: 18px; }
  .shelf .mini { flex: 0 0 116px; cursor: pointer; }
  .shelf .mini img { width: 116px; height: 174px; border-radius: 8px; box-shadow: 0 10px 22px -12px var(--shade); }
  .shelf .mini .mt {
    font-family: var(--serif); font-size: 14px; color: var(--text); margin-top: 8px; line-height: 1.3;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .shelf .mini .mm { font-size: 11.5px; color: var(--faint); margin-top: 2px; }

  /* a novel */
  #view-novel { max-width: 960px; margin: 0 auto; }
  #novel-cover {
    width: 168px; height: 252px; border-radius: 10px; float: left; margin: 0 26px 14px 0;
    box-shadow: 0 18px 36px -18px var(--shade);
  }
  #novel-title { font-family: var(--serif); font-weight: 500; font-size: 34px; line-height: 1.15; margin: 4px 0 8px; }
  #novel-meta { font-size: 14px; margin: 0 0 4px; }
  #novel-synopsis { color: var(--dim); font-size: 15px; line-height: 1.65; margin: 10px 0; max-width: 70ch; }
  #view-novel > button { border-radius: 999px; }
  #continue-btn { padding: 10px 20px; }
  .novel-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 4px 0 18px; clear: both; }
  .novel-tools button { margin: 0; border-radius: 999px; font-size: 13.5px; }
  .rate { font-size: 21px; letter-spacing: 2px; cursor: pointer; color: var(--faint); user-select: none; margin-right: 6px; }
  .rate .on { color: var(--lamp); }
  #shelf-box { margin: 0 0 12px; }
  .chap {
    display: flex; justify-content: space-between; gap: 12px; align-items: center;
    padding: 11px 8px; border-bottom: 1px solid var(--line); cursor: pointer; border-radius: 6px;
  }
  .chap:hover { background: var(--panel); }
  .chap .n { color: var(--faint); font-size: 12.5px; min-width: 52px; font-variant-numeric: tabular-nums; }
  .chap .t { flex: 1; font-size: 14.5px; }
  .chap .rd { color: var(--good); width: 16px; }
  .chap.unavail .t { color: var(--faint); }
  .chap.here .t { color: var(--lamp); font-weight: 600; }
  .back { color: var(--link); cursor: pointer; font-size: 14px; text-decoration: none; }
  .back:hover { text-decoration: underline; }

  /* reading */
  #view-reader { max-width: 720px; margin: 0 auto; }
  #reader-title {
    font-family: var(--serif); font-weight: 500; font-size: 30px; line-height: 1.25; text-align: center;
    margin: 18px 0 34px; text-wrap: balance;
  }
  #reader-content { font-size: 19px; line-height: 1.78; }
  #reader-content.f-serif { font-family: var(--serif); }
  #reader-content p { margin: 0 0 1.05em; }
  #reader-content h1, #reader-content h2, #reader-content h3 { font-family: var(--serif); font-weight: 500; font-size: 1.2em; }
  #reader-content img { max-width: 100%; display: block; margin: 6px auto; border-radius: 4px; }
  #reader-content.fit-h img { max-height: 94vh; width: auto; max-width: 100%; }
  #reader-content.strip img { margin: 0 auto; border-radius: 0; }
  .reader-nav {
    position: sticky; bottom: 14px; display: flex; gap: 10px; align-items: center; margin-top: 28px;
    padding: 8px; background: var(--bar); border: 1px solid var(--line); border-radius: 999px;
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); box-shadow: 0 12px 30px -16px var(--shade);
  }
  .reader-nav button { border-radius: 999px; margin: 0; }
  #reader-content.sp-compact { line-height: 1.55; }
  #reader-content.sp-airy { line-height: 2.05; }
  #reader-content.sp-airy p { margin-bottom: 1.4em; }
  /* a manga page keeps its place while it loads, so the strip does not jump */
  #reader-content img.pending { min-height: 60vh; width: 100%; background: var(--panel); }
  /* how far through the chapter, along the very top */
  #read-progress {
    position: fixed; top: 0; left: 0; height: 3px; width: 0; z-index: 20;
    background: var(--lamp); pointer-events: none; transition: width .12s linear;
  }
  /* while reading, the bars step aside going down and come back going up */
  body.reading .topbar, body.reading .reader-nav { transition: transform .25s ease, opacity .25s ease; }
  body.reading.bars-off .topbar { transform: translateY(-100%); }
  body.reading.bars-off .reader-nav { transform: translateY(170%); opacity: 0; pointer-events: none; }
  #reader-pos { font-size: 13px; font-variant-numeric: tabular-nums; }

  #auth { max-width: 380px; margin: 70px auto; }
  #auth h2 { font-family: var(--serif); font-weight: 500; font-size: 30px; margin: 0 0 6px; }

  /* the Aa panel and the translation pop-up */
  #aa-panel {
    position: fixed; top: 60px; right: 16px; z-index: 30; width: 290px;
    background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
    padding: 14px 16px; box-shadow: 0 18px 40px -16px var(--shade);
  }
  .aa-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 8px 0; flex-wrap: wrap; }
  .aa-label { font-size: 13px; color: var(--dim); }
  #trans-pop {
    position: fixed; z-index: 40; max-width: 300px; font-size: 14px;
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 9px 12px; box-shadow: 0 12px 30px -12px var(--shade);
  }
  #trans-pop .w { color: var(--lamp); font-weight: 600; }

  /* menus, rows, stats */
  .more-menu { position: relative; }
  .more-menu > summary {
    list-style: none; cursor: pointer; padding: 8px 13px; border-radius: 9px;
    border: 1px solid var(--line); font-size: 13.5px; user-select: none; background: var(--panel);
  }
  .more-menu > summary::-webkit-details-marker { display: none; }
  .more-list {
    position: absolute; z-index: 20; top: calc(100% + 6px); left: 0; min-width: 240px;
    display: flex; flex-direction: column; gap: 2px; padding: 6px;
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px; box-shadow: 0 18px 40px -16px var(--shade);
  }
  .more-list button { margin: 0; text-align: left; background: transparent; }
  .more-list button:hover { background: var(--raised); }
  #lib-web-btn { margin: 0 0 10px; }
  .web-row { display: flex; align-items: center; gap: 8px; padding: 8px 4px; border-bottom: 1px solid var(--line); font-size: 13.5px; }
  .web-row .t { flex: 1; min-width: 0; }
  .web-row .t b { color: var(--text); }
  .web-row .t .muted { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .web-row button { padding: 4px 10px; font-size: 12.5px; margin: 0; }
  .bmk-row {
    display: flex; align-items: center; gap: 8px; padding: 8px 6px;
    border-bottom: 1px solid var(--line); font-size: 13.5px; cursor: pointer;
  }
  .bmk-row:hover { background: var(--raised); }
  .bmk-row .x { color: var(--faint); padding: 0 6px; }
  .bmk-row .x:hover { color: var(--bad); }
  .bmk-row .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bmk-row .nv { color: var(--text); font-weight: 600; }
  .bmk-row .when { color: var(--faint); font-size: 12px; white-space: nowrap; }
  .stat-bars { display: flex; align-items: flex-end; gap: 4px; height: 80px; margin: 8px 0 4px; }
  .stat-bars span { flex: 1; background: var(--lamp); border-radius: 3px 3px 0 0; min-height: 2px; }
  .stat-days { display: flex; gap: 4px; font-size: 10px; color: var(--faint); }
  .stat-days span { flex: 1; text-align: center; }
  .shelf-row { display: flex; align-items: center; gap: 8px; padding: 7px 4px; border-bottom: 1px solid var(--line); }
  .shelf-row .n { flex: 1; }
  .shelf-row button { margin: 0; padding: 3px 9px; font-size: 12.5px; }
  .lib-more { height: 1px; }

  /* phones */
  @media (max-width: 640px) {
    .toolbar { gap: 6px; }
    .toolbar select, .toolbar.filters input[type=text] { flex: 1 1 46%; min-width: 0; max-width: none; }
    .toolbar.filters .check { flex: 1 1 46%; }
    .chap-range { flex: 1 1 100%; }
    .more-list { left: auto; right: 0; }
    .novel-tools button { flex: 1 1 46%; }
    .topbar { flex-wrap: wrap; gap: 8px 12px; padding: 8px 14px; padding-top: max(8px, env(safe-area-inset-top)); }
    .topbar h1 { font-size: 17px; }
    .topbar h1 small { display: none; }
    #search-wrap { order: 3; flex: 1 1 100% !important; max-width: none !important; }
    .wrap { padding: 16px 14px 96px; }
    button { padding: 10px 14px; }
    .back { padding: 6px 2px; }
    #lib-list.grid { grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 18px 12px; }
    #lib-list.grid .libcard h3 { font-size: 13.5px; }
    #lib-list.grid .libcard .badge { display: none; }
    #novel-cover { width: 112px; height: 168px; margin: 0 16px 10px 0; }
    #novel-title { font-size: 25px; }
    .libcard .cover { width: 60px; height: 90px; flex-basis: 60px; }
    #reader-title { font-size: 24px; margin: 8px 0 24px; }
    #reader-content { font-size: 18px; }
    .reader-nav { bottom: max(10px, env(safe-area-inset-bottom)); }
    .reader-nav button { padding: 11px 16px; }
  }
  @media (hover: none) {
    .card:active { border-color: var(--faint); }
  }
  @media (prefers-reduced-motion: reduce) {
    * { transition: none !important; }
  }
</style>
</head>
<body>
<div class="topbar">
  <h1>Lightnovel Crawler<small>Reader</small></h1>
  <a class="back hidden" id="nav-back">&larr; Back</a>
  <div class="spacer"></div>
  <div id="search-wrap" style="flex:1;max-width:380px"><input id="search" type="text" placeholder="Search title, author, description, tags…" /></div>
  <a class="back" href="/downloads" target="_blank" id="downloads-link" title="Every download: progress, pause, resume">⤓ Downloads</a>
  <a class="back" href="/tools" target="_blank" id="tools-link">Tools</a>
  <button id="fit-btn" class="hidden" title="Image fit mode">Fit: width</button>
  <button id="aa-btn" class="hidden" title="Reading settings">Aa</button>
</div>

<div id="read-progress" class="hidden"></div>
<div id="aa-panel" class="hidden">
  <div class="aa-row"><span class="aa-label">Size</span>
    <button id="font-dn">A-</button><button id="font-up">A+</button>
  </div>
  <div class="aa-row"><span class="aa-label">Theme</span><span class="tags" id="aa-theme"></span></div>
  <div class="aa-row"><span class="aa-label">Font</span><span class="tags" id="aa-font"></span></div>
  <div class="aa-row"><span class="aa-label">Width</span><span class="tags" id="aa-width"></span></div>
  <div class="aa-row"><span class="aa-label">Spacing</span><span class="tags" id="aa-spacing"></span></div>
  <div class="aa-row" title="How manga pages are shown"><span class="aa-label">Pages</span><span class="tags" id="aa-fit"></span></div>
  <div class="aa-row" title="A tap near the left or right edge scrolls a screen up or down; past the end, the next chapter"><span class="aa-label">Tap edges</span><span class="tags" id="aa-tap"></span></div>
  <div class="aa-row"><span class="aa-label">Tap translate <small class="muted">(to English)</small></span><span class="tags" id="aa-trans"></span></div>
</div>
<div id="trans-pop" class="hidden"></div>

<div class="wrap">
  <div id="auth" class="hidden">
    <h2>Sign in</h2>
    <label for="email">Email</label>
    <input id="email" autocapitalize="none" autocorrect="off" spellcheck="false" type="text" autocomplete="username" />
    <label for="password">Password</label>
    <input id="password" autocapitalize="none" autocorrect="off" spellcheck="false" type="text" autocomplete="current-password" style="-webkit-text-security:disc" />
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
        <option value="popular">Most popular on its site</option>
      </select>
      <button id="lib-layout" title="Show the library as covers or as a list">☰ List</button>
      <button id="lib-filters-btn" title="Show or hide the filters">Filters</button>
      <details class="more-menu" id="lib-more">
        <summary title="More actions">⋯ More</summary>
        <div class="more-list">
          <button id="lib-auto-btn" title="New chapters: automatic checks and notifications">🔔 New chapters</button>
          <button id="lib-bmks-btn" title="Every bookmark, across all novels">🔖 Bookmarks</button>
          <button id="lib-update" title="Re-check every novel on its source for new chapters">⟳ Check updates</button>
          <button id="lib-rank" title="Read each site's own popularity ranking (NovelFire, MangaDex, Weeb Central), so the library can be sorted by it and chapters are got from the most popular down">📈 Read site rankings</button>
          <button id="lib-fill" class="hidden" title="Re-read the details (status, genres, description) of novels that are missing them">✚ Fill in details</button>
          <button id="lib-stats-btn" title="How much you have been reading">📊 Reading stats</button>
          <button id="lib-shelves-btn" title="Rename, delete or reorder your shelves">📚 Manage shelves</button>
          <button id="lib-backup" title="Save your positions, favorites, bookmarks, ratings, views and shelves to a file">⤓ Back up reading data</button>
          <button id="lib-restore" title="Load a backup made here, adding it to what you have">⤒ Restore a backup</button>
          <input id="lib-restore-file" type="file" accept="application/json,.json" class="hidden" />
          <button id="lib-getch" class="hidden" title="Download the chapters of every novel that has only its details (added by a discovery)">⬇ Get chapters for details-only novels</button>
          <button id="lib-finish" class="hidden" title="Download the missing chapters of every novel that has only some of its chapters, most popular first">⬇ Finish partly downloaded novels</button>
          <button id="lib-unfinished" title="Delete every novel that has only its details or only some of its chapters downloaded (admins)">🗑 Delete unfinished novels</button>
          <button id="lib-select" title="Select novels to delete or download">☑ Select</button>
        </div>
      </details>
      <button id="lib-getsel" class="hidden">⬇ Get chapters (0)</button>
      <button id="lib-del" class="hidden">🗑 Delete (0)</button>
      <button id="lib-here" class="hidden" title="Copy their books to this computer, to read them without the drive; the drive keeps its copy too">💻 Keep on this computer (0)</button>
      <button id="lib-todrive" class="hidden" title="Move their books to the drive, to free room on this computer">💾 Send to the drive (0)</button>
      <div id="lib-cats" class="tags"></div>
    </div>
    <div class="toolbar filters hidden" id="lib-filters">
      <select id="lib-kind" title="Media type">
        <option value="">All media</option>
        <option value="novel">Novels</option>
        <option value="comic">All comics</option>
        <option value="manga">Manga (Japanese)</option>
        <option value="manhwa">Manhwa (Korean)</option>
        <option value="manhua">Manhua (Chinese)</option>
      </select>
      <select id="lib-dl" title="Show novels by how much of them is downloaded">
        <option value="">Any downloads</option>
        <option value="complete">Fully downloaded</option>
        <option value="started">Hide details-only</option>
        <option value="partial">Partly downloaded</option>
        <option value="none">Details only</option>
      </select>
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
      <label class="check" title="Hide adult novels and comics (tagged Adult, Mature, Smut, Ecchi, Hentai, Erotica…) everywhere in the Reader. Kept on this and your other devices until you untick it."><input id="lib-nonsfw" type="checkbox" /> Hide NSFW</label>
      <label class="check" title="Hide machine-translated (MTL) novels"><input id="lib-nomtl" type="checkbox" /> Hide MTL</label>
      <label class="check" title="Show only novels with no chapters downloaded yet, such as ones a discovery added with just their details"><input id="lib-noch" type="checkbox" /> No chapters yet</label>
      <button id="lib-clear" class="hidden" title="Clear every filter">✕ Clear filters</button>
      <select id="lib-views" title="Apply a saved view"><option value="">Saved views</option></select>
      <button id="lib-view-save" title="Save the current sort and filters under a name">💾 Save view</button>
      <button id="lib-view-del" class="hidden" title="Delete the selected saved view">✕</button>
    </div>
    <div id="lib-unf" class="card hidden">
      <div class="shelf-title">🗑 Delete unfinished novels</div>
      <div id="unf-pick">
        <div id="unf-kinds">
          <label><input type="radio" name="unf-kind" value="details" /> <span id="unf-details">Only their details</span></label>
          <label><input type="radio" name="unf-kind" value="partial" /> <span id="unf-partial">Partly downloaded</span></label>
          <label><input type="radio" name="unf-kind" value="all" checked /> <span id="unf-all">Both</span></label>
        </div>
        <p class="muted" id="unf-note"></p>
        <div class="toolbar" style="margin:8px 0 0">
          <button id="unf-go">🗑 Delete</button>
          <button id="unf-close" class="secondary">Close</button>
        </div>
      </div>
      <div id="unf-progress" class="hidden">
        <div class="bar"><div id="unf-bar"></div></div>
        <div id="unf-text" class="muted"></div>
      </div>
    </div>
    <div id="lib-stats" class="card hidden">
      <div class="shelf-title">📊 Reading stats <span class="muted" id="lib-stats-note"></span></div>
      <div id="lib-stats-body"></div>
    </div>
    <div id="lib-shelves" class="card hidden">
      <div class="shelf-title">📚 Your shelves</div>
      <div id="lib-shelves-list"></div>
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
    <span id="lib-web-row" class="hidden">
      <button id="lib-web-btn" title="Look for this on the sources the app can download from"></button>
      <select id="lib-web-scope" title="Which sources to search">
        <option value="mine">on the sources in my library</option>
        <option value="all">on every source (slower)</option>
      </select>
    </span>
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
    <button id="getch-btn" class="hidden" style="margin-bottom:12px">⬇ Download all chapters</button>
    <button id="repair-btn" style="margin-bottom:12px">Retry missing chapters and rebuild EPUB</button>
    <button id="repair-download" class="hidden" style="margin-bottom:12px">Download repaired EPUB</button>
    <button id="skip-btn" class="hidden secondary" style="margin-bottom:12px" title="These chapters were tried and could not be had (the site gives an empty page, or the chapter is only on another site). Do without them: the novel then counts as complete and its e-book is made without them.">Do without them</button>
    <a id="unskip-link" class="see-all hidden" title="Count the chapters done without as missing again, so repairs try them">count skipped as missing</a>
    <p id="repair-status" class="muted" aria-live="polite"></p>
    <div id="novel-tags" class="tags"></div>
    <p id="novel-synopsis"></p>
    <div style="clear:both"></div>
    <button id="continue-btn" class="primary hidden" style="margin-bottom:12px">▶ Continue reading</button>
    <button id="fav-btn" style="margin:0 0 12px 8px" title="Add to favorites">☆ Favorite</button>
    <button id="heal-btn" class="hidden" style="margin:0 0 12px 8px" title="Get the missing chapters: from a copy in your library, then this novel's own site, then other sites that have it">✨ Fill missing chapters</button>
    <button id="deep-btn" class="hidden" style="margin:0 0 12px 8px" title="Search other sites for the missing chapters and download them">🔍 Find missing on other sites</button>
    <button id="merge-btn" class="hidden" style="margin:0 0 12px 8px" title="Combine all copies of this title into the most complete one">⧉ Merge copies</button>
    <button id="del-btn" style="margin:0 0 12px 8px" title="Delete this novel from your library">🗑</button>
    <div class="novel-tools">
      <span id="rate-box" class="rate" title="Your rating"></span>
      <button id="prog-done" class="secondary" title="Count this novel as read to the end">✓ Mark finished</button>
      <button id="prog-unread" class="secondary" title="Forget where you were">↺ Mark unread</button>
      <button id="prog-set" class="secondary" title="Say which chapter you are on (read elsewhere)">✎ Set chapter…</button>
      <button id="shelf-btn" class="secondary" title="Put this novel on your shelves">📚 Shelves</button>
      <button id="ebook-btn" class="secondary" title="Save this novel as an e-book file on this device, to read in any e-book app without a connection">📱 Download e-book</button>
      <button id="offline-btn" class="secondary" title="Keep chapters on this device to read without a connection">⬇ Save for offline</button>
      <span id="offline-msg" class="muted"></span>
    </div>
    <div id="shelf-box" class="tags hidden"></div>
    <span class="muted hidden" id="heal-msg" style="margin-left:8px"></span>
    <div id="bmk-list"></div>
    <div id="chap-jump-wrap" class="hidden" style="margin:10px 0">
      <input id="chap-jump" type="number" min="1" inputmode="numeric" placeholder="Go to chapter #" style="width:150px" />
      <button id="chap-jump-go" class="secondary">Go</button>
      <button id="chap-jump-here" class="secondary hidden">Where I am</button>
      <button id="chap-jump-start" class="secondary hidden">From the start</button>
    </div>
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

  // Job polling backs off while the tab is hidden: a background tab polls at
  // most every 30 s instead of every 2-3 s, and catches up as soon as it is shown.
  var waiting = [];
  function later(fn, ms) {
    var entry = { fn: fn, t: 0 };
    entry.t = setTimeout(function () { unqueue(entry); fn(); }, document.hidden ? Math.max(ms, 30000) : ms);
    waiting.push(entry);
    return entry;
  }
  function unqueue(entry) { var i = waiting.indexOf(entry); if (i >= 0) waiting.splice(i, 1); }
  function cancelLater(entry) { if (entry) { clearTimeout(entry.t); unqueue(entry); } }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) return;
    var due = waiting; waiting = [];
    due.forEach(function (e) { clearTimeout(e.t); e.fn(); });
  });

  // While the library renders, positions are parsed from storage once instead
  // of once per novel (and per sort comparison).
  var posMemo = null;
  function loadPos() { try { return JSON.parse(localStorage.getItem(POS) || "{}"); } catch (e) { return {}; } }
  function posOf(novelId) {
    var v = (posMemo || loadPos())[novelId];
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
  function saveFavs(a) {
    var before = loadFavs();
    localStorage.setItem(FAV, JSON.stringify(a));
    var changes = {};
    a.forEach(function (id) { if (before.indexOf(id) < 0) changes[id] = { on: true }; });
    before.forEach(function (id) { if (a.indexOf(id) < 0) changes[id] = { on: false }; });
    markItems("favitems", changes);
  }
  function loadBmks() { try { return JSON.parse(localStorage.getItem(BMK) || "{}"); } catch (e) { return {}; } }
  function bmkKey(novelId, b) { return novelId + "|" + b.id + "|" + b.ts; }
  function flatBmks(m) {
    var out = {};
    Object.keys(m || {}).forEach(function (nid) {
      (m[nid] || []).forEach(function (b) { out[bmkKey(nid, b)] = { novel: nid, b: b }; });
    });
    return out;
  }
  function saveBmks(m) {
    var before = flatBmks(loadBmks()), after = flatBmks(m);
    localStorage.setItem(BMK, JSON.stringify(m));
    var changes = {};
    Object.keys(after).forEach(function (k) { if (!before[k]) changes[k] = { on: true, novel: after[k].novel, b: after[k].b }; });
    Object.keys(before).forEach(function (k) { if (!after[k]) changes[k] = { on: false }; });
    markItems("bmkitems", changes);
  }
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
      lenMax: els["lib-len-max"].value, nomtl: els["lib-nomtl"].checked, noch: els["lib-noch"].checked,
      dl: els["lib-dl"].value
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
    els["lib-noch"].checked = !!v.noch;
    els["lib-dl"].value = v.dl || "";
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
  function saveRatings(m) {
    var before = loadRatings();
    localStorage.setItem(RATE, JSON.stringify(m));
    var changes = {};
    Object.keys(m).forEach(function (id) { if (before[id] !== m[id]) changes[id] = { v: m[id] }; });
    Object.keys(before).forEach(function (id) { if (!(id in m)) changes[id] = { v: 0 }; });
    markItems("rateitems", changes);
  }

  // Favorites, bookmarks and ratings also live here as one entry per item, each
  // with the time it last changed (a removal kept as "off"), and reading stats
  // as one entry per device and day. The server merges these entry by entry, so
  // changes to different items on two devices both survive.
  var ITEMS = "lncrawl_reader_items";  // { favitems, bmkitems, rateitems, stats }
  var KEYED = ["favitems", "bmkitems", "rateitems", "stats"];
  function loadItems() {
    var it; try { it = JSON.parse(localStorage.getItem(ITEMS) || "null"); } catch (e) { it = null; }
    if (!it) {
      // first run: what this device already has, dated as old as possible so any
      // change made elsewhere wins over it
      it = { favitems: {}, bmkitems: {}, rateitems: {}, stats: {} };
      loadFavs().forEach(function (id) { it.favitems[id] = { on: true, ts: 1 }; });
      var fb = flatBmks(loadBmks());
      Object.keys(fb).forEach(function (k) { it.bmkitems[k] = { on: true, ts: 1, novel: fb[k].novel, b: fb[k].b }; });
      var r = loadRatings();
      Object.keys(r).forEach(function (id) { it.rateitems[id] = { v: r[id], ts: 1 }; });
      localStorage.setItem(ITEMS, JSON.stringify(it));
    }
    KEYED.forEach(function (k) { it[k] = it[k] || {}; });
    return it;
  }
  function markItems(section, changes) {
    var keys = Object.keys(changes);
    if (!keys.length) return;
    var it = loadItems(), now = Date.now();
    keys.forEach(function (k) { changes[k].ts = now; it[section][k] = changes[k]; });
    localStorage.setItem(ITEMS, JSON.stringify(it));
    schedulePush();
  }
  // Rebuild the plain lists the page reads from the item entries.
  function applyItems(it) {
    var favs = loadFavs().filter(function (id) { return it.favitems[id] ? it.favitems[id].on : true; });
    Object.keys(it.favitems).forEach(function (id) {
      if (it.favitems[id].on && favs.indexOf(id) < 0) favs.push(id);
    });
    localStorage.setItem(FAV, JSON.stringify(favs));
    var bm = {};
    Object.keys(it.bmkitems).forEach(function (k) {
      var e = it.bmkitems[k];
      if (e.on && e.novel && e.b) (bm[e.novel] = bm[e.novel] || []).push(e.b);
    });
    Object.keys(bm).forEach(function (nid) { bm[nid].sort(function (a, b) { return (a.ts || 0) - (b.ts || 0); }); });
    localStorage.setItem(BMK, JSON.stringify(bm));
    var rt = {};
    Object.keys(it.rateitems).forEach(function (id) { if (it.rateitems[id].v) rt[id] = it.rateitems[id].v; });
    localStorage.setItem(RATE, JSON.stringify(rt));
  }
  function loadAuto() { try { return JSON.parse(localStorage.getItem(AUTO) || "{}"); } catch (e) { return {}; } }
  function saveAuto(m) { localStorage.setItem(AUTO, JSON.stringify(m)); touchSync("autoupdate"); }
  var SECTIONS = {
    prefs: { load: function () { return loadPrefs(); }, key: function () { return PREFS; } },
    updates: { load: function () { return loadUpdates(); }, key: function () { return UPD; } },
    autoupdate: { load: function () { return loadAuto(); }, key: function () { return AUTO; } },
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
    var it = loadItems();
    KEYED.forEach(function (k) { out[k] = it[k]; });
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
    var it = loadItems();
    KEYED.forEach(function (k) {
      var theirs = server[k] || {};
      Object.keys(theirs).forEach(function (key) {
        var mine = it[k][key];
        if (!mine || (theirs[key].ts || 0) > (mine.ts || 0)) it[k][key] = theirs[key];
      });
      Object.keys(it[k]).forEach(function (key) {
        if (!theirs[key] || (it[k][key].ts || 0) > (theirs[key].ts || 0)) dirty = true;
      });
    });
    localStorage.setItem(ITEMS, JSON.stringify(it));
    applyItems(it);
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
  ["nav-back","search","lib-finish","lib-rank","skip-btn","unskip-link","read-progress","aa-spacing","aa-fit","aa-tap","chap-jump-wrap","chap-jump","chap-jump-go","chap-jump-here","chap-jump-start","view-library","lib-continue-wrap","lib-continue","lib-continue-all","lib-sort","lib-kind","lib-tag","lib-tag-list","lib-source","lib-lang","lib-progress","lib-state","lib-favonly","lib-len","lib-len-custom","lib-len-min","lib-len-max","lib-nobl","lib-nonsfw","lib-dupes","lib-shelf","lib-filters-btn","lib-filters","lib-more","lib-stats-btn","lib-stats","lib-stats-note","lib-stats-body","lib-shelves-btn","lib-shelves","lib-shelves-list","lib-backup","lib-restore","lib-restore-file","lib-auto-btn","lib-auto","auto-on","auto-scope","auto-dl","auto-notify","auto-note","rate-box","prog-done","prog-unread","prog-set","shelf-btn","shelf-box","lib-fill","lib-views","lib-view-save","lib-view-del","lib-web-btn","lib-web-row","lib-web-scope","offline-btn","ebook-btn","offline-msg","lib-web","lib-web-status","lib-web-stop","lib-web-list","lib-bmks-btn","lib-bmks","lib-bmks-count","lib-bmks-list","lib-nomtl","lib-noch","lib-dl","lib-layout","lib-getch","lib-unfinished","lib-unf","unf-pick","unf-details","unf-partial","unf-all","unf-note","unf-go","unf-close","unf-progress","unf-bar","unf-text","lib-getsel","lib-here","lib-todrive","getch-btn","lib-clear","lib-cats",
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
    document.body.classList.toggle("reading", inReader);
    document.body.classList.remove("bars-off");
    els["read-progress"].classList.toggle("hidden", !inReader);
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
    img.decoding = "async";
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
    // an image that is not shown is never "in view": fetch those straight away
    if (coverCache[novelId] || !coverWatch || img.classList.contains("hidden")) { fetchCover(img, novelId); return; }
    img.dataset.novel = novelId;
    coverWatch.observe(img);
  }

  // The server hands out at most 100 novels a request, so the library is read a
  // page at a time until every novel is in, however large it has grown. The
  // first page says how many there are; the rest are asked for four at a time
  // (one after another, 1,900 novels were 19 round trips in a row).
  function fetchAllNovels(q) {
    function page(offset) {
      return api("/novels?limit=100&offset=" + offset + "&search=" + encodeURIComponent(q || ""));
    }
    return page(0).then(function (first) {
      var items = (first && first.items) || [];
      var total = (first && first.total) || 0;
      if (items.length < 100 || items.length >= total) return items;
      var offsets = [];
      for (var at = items.length; at < total; at += 100) offsets.push(at);
      var pages = new Array(offsets.length), next = 0, loaded = items.length;
      function worker() {
        if (next >= offsets.length) return Promise.resolve();
        var i = next++;
        return page(offsets[i]).then(function (res) {
          pages[i] = (res && res.items) || [];
          loaded += pages[i].length;
          els["lib-status"].textContent = "Loading your library… " + loaded + " of " + total;
          return worker();
        });
      }
      var workers = [];
      for (var w = 0; w < Math.min(4, offsets.length); w++) workers.push(worker());
      return Promise.all(workers).then(function () { return items.concat.apply(items, pages); });
    });
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
    loadDownloadedCounts();
    loadBooks();
    loadShelves();
    fetchAllNovels("").then(function (items) {
      lib.all = items;
      lib.everything = items;
      lib.hay = {};
      renderContinue();
      renderFilterOptions();
      renderCategories();
      renderLibrary();
      if (lib.got) renderGetChapters();  // the counts may have come before the novels
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
      if (n && pe && progressOf(n) !== "finished" && !(hideNSFW() && isNSFW(n))) recent.push({ n: n, pe: pe });
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

  // Adult content goes by many names across sources (MangaDex adds its own
  // content rating as a tag). Compared whole, letters and digits only, so
  // "Mature" matches but "Maturing" does not.
  var NSFW_TAGS = ["adult", "adultcontent", "mature", "smut", "ecchi", "hentai", "erotica", "erotic",
    "pornographic", "porn", "nsfw", "r18", "18", "sexualcontent", "sexualviolence", "lolicon", "shotacon"];
  function isNSFW(n) {
    return (n.tags || []).some(function (t) {
      return NSFW_TAGS.indexOf(String(t).toLowerCase().replace(/[^a-z0-9]/g, "")) >= 0;
    });
  }
  // An option, not a filter: kept with the reading settings (synced between
  // devices), and Clear filters leaves it on.
  function hideNSFW() { return (prefs || loadPrefs()).nsfw === "hide"; }
  function sfw(list) { return hideNSFW() ? list.filter(function (n) { return !isNSFW(n); }) : list; }

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

  // How many filters are on, for the Filters button.
  function activeFilterCount() {
    return [els["lib-kind"].value, els["lib-dl"].value, els["lib-tag"].value.trim(), els["lib-source"].value, els["lib-lang"].value,
      els["lib-progress"].value, els["lib-shelf"].value, els["lib-state"].value,
      els["lib-favonly"].checked, !!chapterRange(), els["lib-dupes"].checked,
      els["lib-nobl"].checked, els["lib-nomtl"].checked, els["lib-noch"].checked, lib.cat].filter(Boolean).length;
  }
  var FILTERS_OPEN = "lncrawl_reader_filters_open";
  function renderFiltersButton() {
    var n = activeFilterCount();
    var open = !els["lib-filters"].classList.contains("hidden");
    els["lib-filters-btn"].textContent = (open ? "▾ " : "▸ ") + "Filters" + (n ? " (" + n + ")" : "");
    els["lib-filters-btn"].classList.toggle("on", n > 0);
  }
  els["lib-filters-btn"].addEventListener("click", function () {
    var open = els["lib-filters"].classList.toggle("hidden") === false;
    try { localStorage.setItem(FILTERS_OPEN, open ? "1" : ""); } catch (e) { /* private mode */ }
    renderFiltersButton();
  });
  // the More menu closes once something in it is picked, or on a click elsewhere
  els["lib-more"].addEventListener("click", function (e) {
    if (e.target.tagName === "BUTTON") els["lib-more"].open = false;
  });
  document.addEventListener("click", function (e) {
    if (els["lib-more"].open && !els["lib-more"].contains(e.target)) els["lib-more"].open = false;
  });

  function filtersActive() {
    return !!(els["lib-kind"].value || els["lib-dl"].value || els["lib-tag"].value.trim() || els["lib-source"].value ||
      els["lib-state"].value || els["lib-progress"].value || els["lib-favonly"].checked || !!chapterRange() || !!els["lib-shelf"].value || els["lib-nobl"].checked || els["lib-dupes"].checked ||
      els["lib-lang"].value || els["lib-nomtl"].checked || els["lib-noch"].checked || lib.cat);
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
    posMemo = loadPos();
    try { renderLibraryNow(); } finally { posMemo = null; }
  }
  function renderLibraryNow() {
    saveView();
    renderAllBookmarks();  // keeps the Bookmarks count current after reading
    updateWebButton();
    updateFillButton();
    renderAuto();
    renderFiltersButton();
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
    var beforeNSFW = items.length;
    items = sfw(items);
    lib.nsfwHidden = beforeNSFW - items.length;
    if (els["lib-nomtl"].checked) items = items.filter(function (n) { return !n.mtl; });
    if (els["lib-noch"].checked) items = items.filter(detailsOnly);
    var dl = els["lib-dl"].value;
    if (dl && lib.got) items = items.filter(function (n) { return downloadState(n) === dl || (dl === "started" && downloadState(n) !== "none"); });
    els["lib-clear"].classList.toggle("hidden", !filtersActive());

    var sort = els["lib-sort"].value;
    var byText = function (a, b, key) {
      // empty values last, then natural order so "Vol 2" comes before "Vol 10"
      var x = a[key] || "", y = b[key] || "";
      if (!x !== !y) return x ? -1 : 1;
      return x.localeCompare(y, undefined, { numeric: true, sensitivity: "base" });
    };
    var readAt = function (n) { var p = posOf(n.id); return (p && p.ts) || 0; };
    var ratings = sort === "rating" ? loadRatings() : {};
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
      else if (sort === "rating") d = (ratings[b.id] || 0) - (ratings[a.id] || 0);
      else if (sort === "newch") d = ((lib.latest || {})[b.id] || 0) - ((lib.latest || {})[a.id] || 0);
      else if (sort === "status") d = statusRank(a, ONGOING_FIRST) - statusRank(b, ONGOING_FIRST);
      else if (sort === "status-done") d = statusRank(a, COMPLETED_FIRST) - statusRank(b, COMPLETED_FIRST);
      else if (sort === "popular") d = rankOf(a) - rankOf(b);
      else d = (b.created_at || 0) - (a.created_at || 0);  // recent
      return d || byText(a, b, "title");
    });

    els["lib-list"].innerHTML = "";
    if (!items.length) {
      els["lib-status"].textContent = lib.all.length
        ? (words.length ? "Nothing in your library matches “" + q.trim() + "”." : "No matches.")
        : "Your library is empty — download something first (Tools).";
      if (lib.nsfwHidden && lib.all.length) els["lib-status"].textContent += " (" + lib.nsfwHidden + " NSFW hidden)";
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
      (lib.nsfwHidden ? " · " + lib.nsfwHidden + " NSFW hidden" : "") +
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
      c.className = "card libcard" + (unreachable(n) ? " away" : "");

      var img = document.createElement("img"); img.className = "cover"; img.alt = "";
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
      if (detailsOnly(n)) {
        var xb = document.createElement("span"); xb.className = "badge nochap";
        xb.textContent = "details only"; xb.title = "No chapters downloaded yet — open it, or use ⋯ More, to get them";
        h.appendChild(xb);
      }
      if (unreachable(n)) {
        var ab = document.createElement("span"); ab.className = "badge away";
        ab.textContent = "💾 on the drive";
        ab.title = "Its books are on " + lib.books.folder + ", which is not plugged in";
        h.appendChild(ab);
      }
      if (lib.got && downloadState(n) === "partial") {
        var pb = document.createElement("span"); pb.className = "badge part";
        pb.textContent = lib.got[n.id] + "/" + (n.chapter_count || 0) + " downloaded";
        pb.title = "Not every chapter is downloaded yet";
        h.appendChild(pb);
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
        var line = document.createElement("div"); line.className = "meta resume";
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
      if (lib.manage && lib.sel[n.id]) c.style.outline = "2px solid var(--lamp)";
      c.addEventListener("click", function () {
        if (!lib.manage) { openNovel(n); return; }
        if (lib.sel[n.id]) { delete lib.sel[n.id]; c.style.outline = ""; }
        else { lib.sel[n.id] = true; c.style.outline = "2px solid var(--lamp)"; }
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
    els["lib-getsel"].textContent = "⬇ Get chapters (" + n + ")";
    els["lib-getsel"].classList.toggle("hidden", !lib.manage);
    // shown in Select mode whether or not the books have a drive yet: a button
    // that was not there left people looking for it (clicking it says how)
    els["lib-here"].textContent = "💻 Keep on this computer (" + n + ")";
    els["lib-here"].classList.toggle("hidden", !lib.manage);
    els["lib-todrive"].textContent = "💾 Send to the drive (" + n + ")";
    els["lib-todrive"].classList.toggle("hidden", !lib.manage);
  }

  // ---- Books kept on a drive that is not always plugged in ----
  // lib.books: where they are ({configured, plugged, folder, on_drive: [ids]}).
  function loadBooks() {
    return api("/books").then(function (b) {
      b = b || {};
      b.away = {};
      (b.on_drive || []).forEach(function (id) { b.away[id] = true; });
      lib.books = b;
      if (lib.all.length) renderLibrary();
      updateDelBtn();
    }).catch(function () { lib.books = null; });
  }
  function unreachable(n) {
    return !!(lib.books && lib.books.configured && !lib.books.plugged && lib.books.away[n.id]);
  }
  function moveBooks(to, btn) {
    if (!lib.books || !lib.books.configured) {
      alert("Your books are not on a drive of their own yet, so there is nothing to " +
        (to === "drive" ? "send to it" : "bring to this computer") + ".\\n\\n" +
        "Set it up in Tools, Library and drives, \\"Books on a drive that is not always plugged in\\": " +
        "with the library on an external drive, \\"Keep the library here\\" (then close and reopen the app); " +
        "with it on this computer, choose a folder on the drive for the books.");
      return;
    }
    var ids = Object.keys(lib.sel);
    if (!ids.length) { alert("Pick the novels first: click their covers."); return; }
    if (!lib.books.plugged) { alert("Plug in the drive with the books (" + lib.books.folder + ") first."); return; }
    btn.disabled = true;
    api("/books/move", { method: "POST", body: JSON.stringify({ ids: ids, to: to }) }).then(function (b) {
      var m = (b && b.moving) || {};
      alert(to === "drive"
        ? "Moving the books of " + m.total + " novel(s) to the drive (with anything new read or downloaded here). It carries on in the background; they can be read meanwhile."
        : "Copying the books of " + m.total + " novel(s) to this computer. The drive keeps its copy too, so it still holds your whole library. It carries on in the background; they can be read meanwhile.");
      lib.manage = false; lib.sel = {};
      els["lib-select"].textContent = "☑ Select";
      renderLibrary(); updateDelBtn();
      (function watch() {
        api("/books").then(function (s) {
          if (s && s.moving && s.moving.running) { setTimeout(watch, 3000); return; }
          loadBooks();
        }).catch(function () {});
      })();
    }).catch(function (e) { alert("Couldn't move them: " + e.message); })
      .then(function () { btn.disabled = false; });
  }
  els["lib-here"].addEventListener("click", function () { moveBooks("here", this); });
  els["lib-todrive"].addEventListener("click", function () { moveBooks("drive", this); });

  // ---- Novels with only their details (a discovery adds those) ----
  // lib.got is {novel_id: chapters downloaded}; until it loads nothing is marked.
  function loadDownloadedCounts() {
    return api("/novels/downloaded-counts").then(function (m) {
      m = m || {};
      var same = lib.got && JSON.stringify(lib.got) === JSON.stringify(m);
      lib.got = m;
      if (!same && lib.all.length) renderLibrary();  // nothing new: not drawn again
      renderGetChapters();
    }).catch(function () { /* a failed ask keeps what was known: it said "0 details-only" */ });
  }
  function detailsOnly(n) {
    return !!lib.got && !lib.got[n.id];
  }
  // "complete", "partial" or "none": how much of a novel is downloaded
  function downloadState(n) {
    // chapters done without (they could not be had) count as there
    var skipped = (n.extra && Array.isArray(n.extra.skipped)) ? n.extra.skipped.length : 0;
    var got = (lib.got && lib.got[n.id]) || 0, total = n.chapter_count || 0;
    if (got) got += skipped;
    if (!got) return "none";
    return got >= total ? "complete" : "partial";
  }
  function renderGetChapters() {
    var n = (lib.everything || []).filter(detailsOnly).length;
    els["lib-getch"].textContent = "⬇ Get chapters for " + n + " details-only novel(s)";
    els["lib-getch"].classList.toggle("hidden", !n);
    var part = partlyDownloaded().length;
    els["lib-finish"].textContent = "⬇ Finish " + part + " partly downloaded novel(s)";
    els["lib-finish"].classList.toggle("hidden", !part);
  }
  function partlyDownloaded() {
    if (!lib.got) return [];
    return (lib.everything || []).filter(function (n) { return downloadState(n) === "partial" && !unreachable(n); });
  }
  // The bulk download engine gets every chapter of these novels: one job per
  // site, which tunes its speed to the site, waits out blocks and hangs, and
  // carries on after a restart. Chapters already downloaded are skipped.
  function getChapters(novels, done) {
    var urls = novels.map(function (n) { return n.url; }).filter(Boolean);
    if (!urls.length) { if (done) done(null); return; }
    api("/job/create/bulk-download", { method: "POST", body: JSON.stringify({ urls: urls }) })
      .then(function (jobs) {
        jobs = Array.isArray(jobs) ? jobs : [jobs];
        alert("Downloading the chapters of " + urls.length + " novel(s) (" + jobs.length + " site(s)). " +
          "It keeps going on its own, waiting out sites that block it, so you can leave it; " +
          "novels fill in as they arrive.");
        if (done) done(jobs[0]);
        jobs.forEach(function (job) { watchGetChapters(job.id); });
      })
      .catch(function (e) { alert("Couldn't start the download: " + e.message); if (done) done(null); });
  }
  // refresh the marks while the download runs, so novels lose "details only" as they fill
  // One timer for all of them (one job per site used to mean one loop each, every
  // loop redrawing the whole library), quiet while the page is hidden.
  var getchWatch = { ids: {}, timer: null };
  function watchGetChapters(jobId) {
    getchWatch.ids[jobId] = 1;
    if (!getchWatch.timer) getchWatch.timer = later(tickGetChapters, 15000);
  }
  function tickGetChapters() {
    getchWatch.timer = null;
    var ids = Object.keys(getchWatch.ids);
    if (!ids.length) return;
    if (document.visibilityState === "hidden") { getchWatch.timer = later(tickGetChapters, 15000); return; }
    Promise.all(ids.map(function (id) {
      return api("/job/" + id + "?light=true").then(function (job) {
        var st = job ? statusName(job.status) : "";
        if (st !== "PENDING" && st !== "RUNNING") delete getchWatch.ids[id];
      }).catch(function (e) { if (/No such job/.test((e && e.message) || "")) delete getchWatch.ids[id]; });
    })).then(function () {
      loadDownloadedCounts();
      if (Object.keys(getchWatch.ids).length) getchWatch.timer = later(tickGetChapters, 15000);
    });
  }
  // A novel's place on its site's popularity ranking (unranked ones last).
  function rankOf(n) {
    var r = n && n.extra && n.extra.rank;
    return typeof r === "number" && r > 0 ? r : Infinity;
  }
  function byRank(list) {
    return list.slice().sort(function (a, b) {
      var d = rankOf(a) - rankOf(b);
      return d === d ? d : 0;  // two unranked (Infinity - Infinity) keep their order
    });
  }
  els["lib-getch"].addEventListener("click", function () {
    var todo = byRank((lib.everything || []).filter(detailsOnly));
    if (!todo.length) return;
    var ranked = todo.filter(function (n) { return rankOf(n) !== Infinity; }).length;
    var answer = prompt("Get the chapters of how many details-only novels, from the most popular down? " +
      "(" + todo.length + " in all; " + (ranked ? ranked + " have a site ranking" :
        "none has a site ranking yet: More › Read site rankings first") + ". Leave empty for all.)", ranked ? String(Math.min(ranked, 50)) : "");
    if (answer === null) return;
    var n = parseInt(answer, 10);
    getChapters(n > 0 ? todo.slice(0, n) : todo);
  });
  els["lib-finish"].addEventListener("click", function () {
    var todo = byRank(partlyDownloaded());
    if (!todo.length) return;
    if (!confirm("Download the missing chapters of " + todo.length + " partly downloaded novel(s)? " +
        "Chapters already here are skipped. Ones a site cannot give stay missing: open such a novel to do without them.")) return;
    getChapters(todo);
  });
  // ---- Site rankings ----
  var rankTimer = null;
  function showRanking(s) {
    var btn = els["lib-rank"];
    if (s.running) {
      btn.textContent = "📈 Reading rankings… " + (s.site || "") + " · " + s.looked + " looked at, " + s.ranked + " of yours found";
      return;
    }
    btn.textContent = "📈 Read site rankings";
  }
  els["lib-rank"].addEventListener("click", function () {
    api("/admin/ranks").then(function (r) {
      var sites = Object.keys(r.sites || {});
      if (r.reading && r.reading.running) { watchRanking(); return; }
      if (!sites.length) { alert("None of your library's sites has a ranking the app can read yet (NovelFire, MangaDex and Weeb Central do)."); return; }
      if (!confirm("Read the popularity ranking of " + sites.join(", ") + "? It runs in the background at each site's own pace (NovelFire's full list takes 15 minutes or so). Then sort by Most popular, and details-only novels get their chapters from the top down.")) return;
      return api("/admin/ranks", { method: "POST" }).then(function () { watchRanking(); });
    }).catch(function (e) { alert("Couldn't read the rankings: " + e.message); });
  });
  function watchRanking() {
    if (rankTimer) return;
    (function tick() {
      api("/admin/ranks").then(function (r) {
        var s = r.reading || {};
        showRanking(s);
        if (s.running) { rankTimer = later(function () { rankTimer = null; tick(); }, 4000); return; }
        rankTimer = null;
        loadLibrary();
        els["lib-sort"].value = "popular"; renderLibrary();
        if (s.error) alert("Rankings read, with a problem: " + s.error);
      }).catch(function () { rankTimer = null; });
    })();
  }
  // Everything not fully downloaded goes, or only one kind of it, except what a
  // download still running, queued or paused is working on: the server says
  // which and how many. A bar shows how far the deleting has got.
  var unf = null;  // what the server last said
  function unfKind() {
    var on = document.querySelector('input[name="unf-kind"]:checked');
    return on ? on.value : "all";
  }
  function unfCount(kind) {
    if (!unf) return 0;
    return kind === "details" ? unf.details_only : kind === "partial" ? unf.partial : unf.count;
  }
  function renderUnf() {
    if (!unf) return;
    els["unf-details"].textContent = "Only their details: " + unf.details_only + " novel(s), nothing downloaded";
    els["unf-partial"].textContent = "Partly downloaded: " + unf.partial + " novel(s), " + unf.chapters + " chapter(s) go with them";
    els["unf-all"].textContent = "Both: " + unf.count + " novel(s)";
    var note = "Their reading positions go with them. This cannot be undone.";
    if (unf.kept) note += " " + unf.kept + " unfinished novel(s) are kept: a download still running, queued or paused is working on them (or on their site). Stop it to delete those too.";
    if (unf.behind) note += " " + unf.behind + " novel(s) that were fully downloaded and have new chapters since are not counted: those only need updating.";
    els["unf-note"].textContent = note;
    var n = unfCount(unfKind());
    els["unf-go"].disabled = !n;
    els["unf-go"].textContent = "🗑 Delete " + n + " novel(s)";
  }
  document.querySelectorAll('input[name="unf-kind"]').forEach(function (r) { r.addEventListener("change", renderUnf); });
  els["lib-unfinished"].addEventListener("click", function () {
    els["lib-more"].open = false;
    els["lib-unf"].classList.remove("hidden");
    els["unf-pick"].classList.add("hidden");
    els["unf-progress"].classList.add("hidden");
    api("/novels/unfinished").then(function (found) {
      unf = found;
      if (found.removing && found.removing.running) { watchUnfinished(); return; }
      els["unf-pick"].classList.remove("hidden");
      renderUnf();
    }).catch(function (e) {
      els["lib-unf"].classList.add("hidden");
      alert("Couldn't look for unfinished novels: " + e.message);
    });
  });
  els["unf-close"].addEventListener("click", function () { els["lib-unf"].classList.add("hidden"); });
  els["unf-go"].addEventListener("click", function () {
    var kind = unfKind(), n = unfCount(kind);
    if (!n || !confirm("Delete " + n + " unfinished novel(s)? This cannot be undone.")) return;
    els["unf-go"].disabled = true;
    api("/novels/unfinished/remove?kind=" + kind, { method: "POST" })
      .then(function () { watchUnfinished(); })
      .catch(function (e) { els["unf-go"].disabled = false; alert("Couldn't delete them: " + e.message); });
  });
  function showUnfProgress(r) {
    els["unf-pick"].classList.add("hidden");
    els["unf-progress"].classList.remove("hidden");
    var pct = r.total ? Math.round(100 * r.done / r.total) : 100;
    els["unf-bar"].style.width = pct + "%";
    els["unf-text"].textContent = r.running
      ? "Deleting " + r.done + " of " + r.total + " (" + pct + "%)" + (r.current ? ": " + r.current : "")
      : "Deleted " + (r.done - r.failed) + " of " + r.total + " novel(s)." + (r.failed ? " " + r.failed + " could not be deleted: " + r.error : "");
  }
  // Only the progress while it runs: counting every unfinished novel again
  // reads every chapter row, each second, beside the removal it is watching.
  function watchUnfinished() {
    els["lib-unf"].classList.remove("hidden");
    api("/novels/unfinished?progress_only=true").then(function (progress) {
      var r = (progress && progress.removing) || {};
      showUnfProgress(r);
      if (r.running) { setTimeout(watchUnfinished, 1000); return; }
      return api("/novels/unfinished").then(function (found) {
        showUnfProgress(found.removing || {});
        unf = found;
        loadLibrary(els["search"].value.trim());
        // the choice comes back with the new counts, under what was done
        els["unf-pick"].classList.remove("hidden");
        renderUnf();
      });
    }).catch(function () { setTimeout(watchUnfinished, 3000); });
  }
  els["lib-getsel"].addEventListener("click", function () {
    var ids = lib.sel;
    var todo = (lib.everything || []).filter(function (n) { return ids[n.id]; });
    if (!todo.length) return;
    var btn = this; btn.disabled = true;
    getChapters(todo, function (job) {
      btn.disabled = false;
      if (!job) return;
      lib.manage = false; lib.sel = {};
      els["lib-select"].textContent = "☑ Select";
      updateDelBtn(); renderLibrary();
    });
  });
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
  els["lib-dl"].addEventListener("change", function () { saveView(); renderLibrary(); });
  // covers or a list, remembered on this device
  var LAYOUT = "lncrawl_reader_layout";
  function applyLayout() {
    var list = (function () { try { return localStorage.getItem(LAYOUT) === "list"; } catch (e) { return false; } })();
    els["lib-list"].classList.toggle("grid", !list);
    els["lib-layout"].textContent = list ? "▦ Covers" : "☰ List";
  }
  els["lib-layout"].addEventListener("click", function () {
    var list = els["lib-list"].classList.contains("grid");
    try { localStorage.setItem(LAYOUT, list ? "list" : "grid"); } catch (e) {}
    applyLayout();
  });
  applyLayout();
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
  els["lib-nonsfw"].checked = hideNSFW();
  els["lib-nonsfw"].addEventListener("change", function () {
    prefs.nsfw = els["lib-nonsfw"].checked ? "hide" : "";
    savePrefs(prefs);
    renderContinue(); renderLibrary();
  });
  els["lib-len"].addEventListener("change", function () {
    els["lib-len-custom"].classList.toggle("hidden", this.value !== "custom");
    renderLibrary();
  });
  ["lib-len-min", "lib-len-max"].forEach(function (k) {
    els[k].addEventListener("input", function () { clearTimeout(tagTimer); tagTimer = setTimeout(renderLibrary, 200); });
  });
  els["lib-nomtl"].addEventListener("change", renderLibrary);
  els["lib-noch"].addEventListener("change", renderLibrary);
  var tagTimer = null;
  els["lib-tag"].addEventListener("input", function () {
    clearTimeout(tagTimer);
    tagTimer = setTimeout(renderLibrary, 150);
  });
  els["lib-clear"].addEventListener("click", function () {
    els["lib-kind"].value = ""; els["lib-dl"].value = ""; els["lib-tag"].value = ""; els["lib-source"].value = "";
    els["lib-lang"].value = ""; els["lib-state"].value = ""; els["lib-progress"].value = ""; els["lib-favonly"].checked = false;
    els["lib-len"].value = ""; els["lib-len-min"].value = ""; els["lib-len-max"].value = "";
    els["lib-len-custom"].classList.add("hidden"); els["lib-nobl"].checked = false; els["lib-dupes"].checked = false; els["lib-shelf"].value = ""; els["lib-nomtl"].checked = false; els["lib-noch"].checked = false; lib.cat = "";
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
    els["lib-web-row"].classList.toggle("hidden", q.length < 2);
    var sel = els["lib-web-scope"];
    if (sel.options.length <= 2) {
      // and each source the library uses, to search just that one
      var doms = {};
      (lib.everything || lib.all).forEach(function (n) { if (n.domain) doms[n.domain] = 1; });
      Object.keys(doms).sort().forEach(function (d) {
        var o = document.createElement("option"); o.value = d; o.textContent = "on " + d + " only"; sel.appendChild(o);
      });
    }
    var link = /^https?:[/][/][^/]+[/].+/i.test(q);
    btn.textContent = link ? "⬇ Download this novel" : "🔎 Search the sources for “" + q + "”";
    els["lib-web-scope"].classList.toggle("hidden", link);
  }
  function stopWebSearch() {
    cancelLater(web.timer);
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
    if (/^https?:[/][/][^/]+[/].+/i.test(q)) {
      // a novel's link: download just that novel
      var btn = this; btn.disabled = true;
      api("/job/create/bulk-download", { method: "POST", body: JSON.stringify({ urls: [q] }) })
        .then(function () {
          alert("Downloading this novel into your library. It carries on by itself; it shows up here as soon as its details are in, and chapters arrive as they download.");
          els["search"].value = ""; renderLibrary();
          later(loadLibrary, 15000);
        })
        .catch(function (e) { alert("Could not download it: " + e.message); })
        .finally(function () { btn.disabled = false; });
      return;
    }
    stopWebSearch();
    web.query = q;
    els["lib-web"].classList.remove("hidden");
    els["lib-web-list"].innerHTML = "";
    els["lib-web-status"].textContent = "· searching…";
    var scope = els["lib-web-scope"].value;
    var body = { query: q.slice(0, 50), results_only: true };
    if (scope === "mine") {
      var doms = {};
      (lib.everything || lib.all).forEach(function (n) { if (n.domain) doms[n.domain] = 1; });
      body.domains = Object.keys(doms);
    } else if (scope !== "all") {
      body.domain = scope;
    }
    api("/job/create/search-sources", {
      method: "POST",
      body: JSON.stringify(body)
    }).then(function (job) {
      web.job = job.id;
      els["lib-web-stop"].classList.remove("hidden");
      pollWeb();
    }).catch(function (e) { els["lib-web-status"].textContent = "· couldn't search: " + e.message; });
  });
  function pollWeb() {
    var id = web.job;
    if (!id) return;
    api("/job/" + id + "?light=true").then(function (job) {
      if (web.job !== id) return;
      renderWebResults((job.extra && job.extra.search_results) || []);
      var running = statusName(job.status) === "PENDING" || statusName(job.status) === "RUNNING";
      var n = els["lib-web-list"].children.length;
      els["lib-web-status"].textContent = "· " + n + " found" +
        (running ? " — searching " + (job.done || 0) + "/" + (job.total || "?") + " sources…" : "");
      if (running) web.timer = later(pollWeb, 2000);
      else { web.job = null; els["lib-web-stop"].classList.add("hidden"); }
    }).catch(function (e) {
      if (web.job !== id) return;
      if (jobGone(e)) {
        web.job = null;
        els["lib-web-stop"].classList.add("hidden");
        els["lib-web-status"].textContent = "· " + els["lib-web-list"].children.length + " found — the search is no longer there";
        return;
      }
      web.timer = later(pollWeb, 4000);
    });
  }
  // The server's answer for a job that was deleted.
  function jobGone(e) { return /No such job/.test((e && e.message) || ""); }
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
          api("/job/" + job.id + "?light=true").then(function (j) {
            var st = statusName(j.status);
            if (st === "PENDING" || st === "RUNNING") {
              note.textContent = full ? "downloading… " + (j.done || 0) + "/" + (j.total || "?") : "adding…";
              later(wait, 2500);
              return;
            }
            note.textContent = st === "SUCCESS" ? "✓ added" : "failed" + (j.error ? ": " + String(j.error).split("\\n").pop() : "");
            if (st === "SUCCESS") loadLibrary();
          }).catch(function () { later(wait, 5000); });
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
    var st = lib.autoStatus || {};
    var since = st.last ? lib.all.filter(function (n) { return ((lib.latest || {})[n.id] || 0) > st.last; }).length : 0;
    els["auto-note"].textContent = (a.enabled
      ? "The server checks these once a day" + (a.download ? " and downloads what it finds." : ".")
      : "Off: use ⟳ Check updates to look now.") +
      (st.last ? " Last check " + timeAgo(st.last) + " (" + (st.queued || 0) + " novel(s)); " +
        since + " novel(s) got new chapters since." : (a.enabled ? " It hasn't run yet." : "")) +
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
    api("/settings/auto-update").then(function (st) { lib.autoStatus = st; renderAuto(); }).catch(function () {});
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
  // Shelves in the order you put them (kept with the synced reading settings).
  function orderedShelves() {
    var order = (prefs && prefs.shelfOrder) || [];
    return (lib.shelves || []).slice().sort(function (a, b) {
      var ia = order.indexOf(a.id), ib = order.indexOf(b.id);
      if (ia < 0) ia = 1e6; if (ib < 0) ib = 1e6;
      return ia - ib || a.name.localeCompare(b.name);
    });
  }
  function renderShelvesPanel() {
    var box = els["lib-shelves-list"];
    box.innerHTML = "";
    var list = orderedShelves();
    if (!list.length) {
      box.innerHTML = "<p class='muted'>No shelves yet. Open a novel and press 📚 Shelves → + New shelf.</p>";
      return;
    }
    list.forEach(function (sh, i) {
      var row = document.createElement("div"); row.className = "shelf-row";
      var n = document.createElement("span"); n.className = "n";
      n.textContent = sh.name + " (" + (((lib.membership || {})[sh.id]) || []).length + ")";
      var up = document.createElement("button"); up.className = "secondary"; up.textContent = "↑"; up.disabled = i === 0;
      var dn = document.createElement("button"); dn.className = "secondary"; dn.textContent = "↓"; dn.disabled = i === list.length - 1;
      var rn = document.createElement("button"); rn.className = "secondary"; rn.textContent = "✎ Rename";
      var del = document.createElement("button"); del.className = "secondary"; del.textContent = "🗑";
      function move(by) {
        var ids = list.map(function (x) { return x.id; });
        var j = i + by; var tmp = ids[i]; ids[i] = ids[j]; ids[j] = tmp;
        prefs.shelfOrder = ids; savePrefs(prefs);
        renderShelvesPanel(); renderShelfFilter();
      }
      up.addEventListener("click", function () { move(-1); });
      dn.addEventListener("click", function () { move(1); });
      rn.addEventListener("click", function () {
        var name = (prompt("Rename “" + sh.name + "” to:", sh.name) || "").trim();
        if (!name || name === sh.name) return;
        api("/library/" + sh.id, { method: "PATCH", body: JSON.stringify({ name: name }) })
          .then(loadShelves).then(renderShelvesPanel)
          .catch(function (e) { alert("Couldn't rename it: " + e.message); });
      });
      del.addEventListener("click", function () {
        if (!confirm("Delete the shelf “" + sh.name + "”? The novels on it stay in your library.")) return;
        api("/library/" + sh.id, { method: "DELETE" })
          .then(loadShelves).then(renderShelvesPanel)
          .catch(function (e) { alert("Couldn't delete it: " + e.message); });
      });
      row.appendChild(n); row.appendChild(up); row.appendChild(dn); row.appendChild(rn); row.appendChild(del);
      box.appendChild(row);
    });
  }
  els["lib-shelves-btn"].addEventListener("click", function () {
    els["lib-shelves"].classList.toggle("hidden");
    renderShelvesPanel();
  });

  function shelvesOf(novelId) {
    return (lib.shelves || []).filter(function (s) { return ((lib.membership || {})[s.id] || []).indexOf(novelId) >= 0; });
  }
  function renderShelfFilter() {
    var sel = els["lib-shelf"];
    var keep = lib.pendingShelf || sel.value;
    lib.pendingShelf = null;
    sel.innerHTML = "";
    var all = document.createElement("option"); all.value = ""; all.textContent = "All shelves"; sel.appendChild(all);
    orderedShelves().forEach(function (s) {
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
    orderedShelves().forEach(function (s) {
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

  // ---- Save for offline ----
  // Chapters are fetched now and kept in the Reader's offline cache, the same
  // one the service worker answers from with no connection, so they open later
  // without one: the text, and a manga chapter's pages.
  var OFFLINE_CACHE = "lncrawl-reader-v1";
  function keep(url) {
    return fetch(url, { headers: { "Authorization": "Bearer " + token() } }).then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      var copy = res.clone();
      return caches.open(OFFLINE_CACHE).then(function (c) { return c.put(url, copy); }).then(function () { return res; });
    });
  }
  // A phone opens the Reader at this computer's plain-http Wi-Fi address,
  // where browsers keep nothing for offline use (that needs https, or the
  // device itself): Save for offline is only offered where it can work, and the
  // e-book is what goes to a phone.
  if (!window.isSecureContext || !("caches" in window)) els["offline-btn"].classList.add("hidden");
  els["ebook-btn"].addEventListener("click", function () {
    var novel = current.novel;
    if (!novel) return;
    var btn = this, msg = els["offline-msg"], novelId = novel.id;
    btn.disabled = true;
    msg.textContent = "Making the e-book (a long novel can take a minute)…";
    api("/novel/" + novelId + "/ebook", { method: "POST" }).then(function (artifact) {
      if (current.novel && current.novel.id === novelId) msg.textContent = "Downloading " + artifact.file_name + "…";
      var fileUrl = "/static/" + artifact.output_file.split("/").map(encodeURIComponent).join("/") +
        "?token=" + encodeURIComponent(token());
      // An iPhone or iPad cannot save a file handed to it by the page (it sat on
      // a blank page, loading for ever): it is sent to the file itself, which
      // Safari offers to open in Books or save to Files.
      var apple = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
        (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
      if (apple) {
        if (current.novel && current.novel.id === novelId) {
          msg.textContent = "✓ " + artifact.file_name + " is ready: choose Open in Books, or Download. If nothing happens, open this page in Safari (the compass button) and try again.";
        }
        window.location.href = fileUrl;
        return;
      }
      return fetch(fileUrl, {
        headers: { "Authorization": "Bearer " + token() }
      }).then(function (response) {
        if (!response.ok) throw new Error("Download failed: HTTP " + response.status);
        return response.blob();
      }).then(function (blob) {
        var url = URL.createObjectURL(blob);
        var link = document.createElement("a");
        link.href = url; link.download = artifact.file_name;
        document.body.appendChild(link); link.click(); link.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
        if (current.novel && current.novel.id === novelId) {
          msg.textContent = "✓ Saved " + artifact.file_name + ": open it from your downloads in an e-book app (Google Play Books, Apple Books, …).";
        }
      });
    }).catch(function (e) {
      if (current.novel && current.novel.id === novelId) msg.textContent = "Couldn't make the e-book: " + e.message;
    }).finally(function () { btn.disabled = false; });
  });
  els["offline-btn"].addEventListener("click", function () {
    var novel = current.novel;
    if (!novel) return;
    if (!("caches" in window)) { alert("This browser can't keep pages for offline reading."); return; }
    var pe = posOf(novel.id);
    var from = pe && pe.serial ? pe.serial : 1;
    var raw = prompt("How many chapters to keep, starting at chapter " + from + "? (a number, or “all”)", "20");
    if (!raw) return;
    var want = /^all$/i.test(raw.trim()) ? Infinity : parseInt(raw, 10);
    if (!want || want < 1) return;
    var msg = els["offline-msg"], btn = this, kept = 0, ids = [];
    btn.disabled = true;
    msg.textContent = "Finding chapters…";
    keep("/api/novel/" + novel.id).catch(function () {});
    (function page(offset) {
      return api("/novel/" + novel.id + "/chapters?limit=100&offset=" + offset).then(function (res) {
        var items = (res && res.items) || [];
        keep("/api/novel/" + novel.id + "/chapters?limit=100&offset=" + offset).catch(function () {});
        items.forEach(function (ch) { if (ch.is_available && ids.length < want) ids.push(ch.id); });
        if (items.length === 100 && ids.length < want) return page(offset + 100);
      });
    })(Math.max(0, from - 1)).then(function () {
      var i = 0;
      function next() {
        if (i >= ids.length) {
          btn.disabled = false;
          msg.textContent = "✓ " + kept + " chapter(s) saved on this device.";
          return;
        }
        var id = ids[i++];
        msg.textContent = "Saving " + i + "/" + ids.length + "…";
        keep("/api/chapter/" + id + "/read?auto_fetch=false")
          .then(function (res) { return res.json(); })
          .then(function (data) {
            kept++;
            var body = (data && (data.content || data.body)) || JSON.stringify(data || {});
            var imgs = body.match(/images\\/([0-9a-fA-F]+)\\.jpg/g) || [];
            return Promise.all(imgs.map(function (m) {
              return keep("/api/chapter/image/" + m.slice(7, -4)).catch(function () {});
            }));
          })
          .catch(function () { /* skip a chapter that fails */ })
          .then(next);
      }
      next();
    }).catch(function (e) { btn.disabled = false; msg.textContent = "Couldn't save: " + e.message; });
  });

  // ---- Back up and restore ----
  els["lib-backup"].addEventListener("click", function () {
    var byId = {}; (lib.everything || lib.all).forEach(function (n) { byId[n.id] = n; });
    var shelves = orderedShelves().map(function (sh) {
      return {
        name: sh.name,
        novels: (((lib.membership || {})[sh.id]) || []).map(function (id) {
          var n = byId[id] || {}; return { id: id, url: n.url || "", title: n.title || "" };
        })
      };
    });
    var data = { app: "lncrawl-reader", version: 1, saved: new Date().toISOString(), state: statePayload(), shelves: shelves };
    var blob = new Blob([JSON.stringify(data, null, 1)], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "reader-backup-" + new Date().toISOString().slice(0, 10) + ".json";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
  });
  els["lib-restore"].addEventListener("click", function () { els["lib-restore-file"].click(); });
  els["lib-restore-file"].addEventListener("change", function () {
    var file = this.files && this.files[0];
    this.value = "";
    if (!file) return;
    file.text().then(function (text) {
      var data = JSON.parse(text);
      if (!data || data.app !== "lncrawl-reader" || !data.state) throw new Error("not a Reader backup");
      // added to what is here: newer entries win, nothing here is dropped
      absorbState(data.state, true);
      var byUrl = {}, byId = {};
      (lib.everything || lib.all).forEach(function (n) { byUrl[n.url] = n.id; byId[n.id] = n.id; });
      var shelves = data.shelves || [];
      return shelves.reduce(function (chain, sh) {
        return chain.then(function () {
          var have = (lib.shelves || []).filter(function (x) { return x.name === sh.name; })[0];
          var made = have ? Promise.resolve(have) : api("/library", { method: "POST", body: JSON.stringify({ name: sh.name }) });
          return made.then(function (shelf) {
            var on = ((lib.membership || {})[shelf.id]) || [];
            return (sh.novels || []).reduce(function (c2, nv) {
              var id = byId[nv.id] || byUrl[nv.url];
              if (!id || on.indexOf(id) >= 0) return c2;
              return c2.then(function () { return api("/library/" + shelf.id + "/novel/" + id, { method: "PUT" }).catch(function () {}); });
            }, Promise.resolve());
          });
        });
      }, Promise.resolve()).then(function () { return shelves.length; });
    }).then(function (n) {
      pushState();
      prefs = loadPrefs(); applyPrefs(); renderViews();
      loadLibrary();
      alert("Backup restored" + (n ? " (with " + n + " shelf/shelves)" : "") + ".");
    }).catch(function (e) { alert("Couldn't restore it: " + e.message); });
  });

  // ---- Reading stats ----
  // Chapters opened and time spent in the reader, per day, kept per device and
  // added up across devices.
  var DEVICE = "lncrawl_reader_device";
  function deviceId() {
    var d = localStorage.getItem(DEVICE);
    if (!d) { d = Math.random().toString(36).slice(2, 10); localStorage.setItem(DEVICE, d); }
    return d;
  }
  function dayKey(t) { var d = new Date(t || Date.now()); return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2); }
  function addStat(chapters, ms) {
    var it = loadItems();
    var key = deviceId() + "|" + dayKey();
    var e = it.stats[key] || { chapters: 0, ms: 0 };
    e.chapters += chapters; e.ms += ms; e.ts = Date.now();
    it.stats[key] = e;
    localStorage.setItem(ITEMS, JSON.stringify(it));
    // Reading time ticks every 30 s; sending the whole reading state that often
    // was most of the Reader's traffic. Time alone is sent every few minutes,
    // and when the page is left; a chapter read is sent at once.
    if (chapters || Date.now() - statPushed > STAT_PUSH_EVERY) { statPushed = Date.now(); schedulePush(); }
    else statPending = true;
  }
  var STAT_PUSH_EVERY = 5 * 60 * 1000, statPushed = 0, statPending = false;
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden" && statPending) { statPending = false; statPushed = Date.now(); schedulePush(); }
  });
  setInterval(function () {
    if (document.visibilityState === "visible" && !els["view-reader"].classList.contains("hidden")) addStat(0, 30000);
  }, 30000);
  function statsByDay() {
    var days = {};
    var st = loadItems().stats;
    Object.keys(st).forEach(function (k) {
      var day = k.split("|")[1];
      var d = days[day] || (days[day] = { chapters: 0, ms: 0 });
      d.chapters += st[k].chapters || 0; d.ms += st[k].ms || 0;
    });
    return days;
  }
  function renderStats() {
    var days = statsByDay();
    var sum = function (n) {
      var c = 0, ms = 0;
      for (var i = 0; i < n; i++) { var d = days[dayKey(Date.now() - i * 864e5)]; if (d) { c += d.chapters; ms += d.ms; } }
      return { c: c, ms: ms };
    };
    var hours = function (ms) { return ms >= 36e5 ? (ms / 36e5).toFixed(1) + " h" : Math.round(ms / 6e4) + " min"; };
    var all = { c: 0, ms: 0 };
    Object.keys(days).forEach(function (k) { all.c += days[k].chapters; all.ms += days[k].ms; });
    var streak = 0;
    while (days[dayKey(Date.now() - streak * 864e5)] && days[dayKey(Date.now() - streak * 864e5)].chapters) streak++;
    var w = sum(7), m = sum(30);
    var body = els["lib-stats-body"];
    body.innerHTML = "";
    var line = document.createElement("p");
    line.textContent = "This week: " + w.c + " chapters · " + hours(w.ms) + "   ·   Last 30 days: " + m.c +
      " chapters · " + hours(m.ms) + "   ·   All time: " + all.c + " chapters · " + hours(all.ms) +
      "   ·   Streak: " + streak + " day" + (streak === 1 ? "" : "s");
    body.appendChild(line);
    var bars = document.createElement("div"); bars.className = "stat-bars";
    var labels = document.createElement("div"); labels.className = "stat-days";
    var counts = [];
    for (var i = 13; i >= 0; i--) counts.push({ t: Date.now() - i * 864e5, c: (days[dayKey(Date.now() - i * 864e5)] || {}).chapters || 0 });
    var max = Math.max.apply(null, counts.map(function (x) { return x.c; }).concat([1]));
    counts.forEach(function (x) {
      var b = document.createElement("span"); b.style.height = Math.round(100 * x.c / max) + "%";
      b.title = dayKey(x.t) + ": " + x.c + " chapter(s)"; bars.appendChild(b);
      var l = document.createElement("span"); l.textContent = new Date(x.t).getDate(); labels.appendChild(l);
    });
    body.appendChild(bars); body.appendChild(labels);
    els["lib-stats-note"].textContent = "· chapters opened per day, last 14 days, all your devices";
  }
  els["lib-stats-btn"].addEventListener("click", function () {
    els["lib-stats"].classList.toggle("hidden");
    renderStats();
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
        " downloaded · " + report.missing + " missing · " + report.failed + " failed · " + report.empty + " empty" +
        (report.skipped ? " · " + report.skipped + " done without" : "");
      // the library's badge counted fewer than are here: it is counted again
      if (lib.got && (lib.got[novelId] || 0) !== report.downloaded) loadDownloadedCounts();
      // chapters tried and lost can be done without, so the novel counts as complete
      var lost = (report.failed || 0) + (report.empty || 0);
      els["skip-btn"].textContent = "Do without the " + lost + " that can't be had";
      els["skip-btn"].classList.toggle("hidden", !lost);
      els["unskip-link"].classList.toggle("hidden", !report.skipped);
      els["novel-completeness"].title = (report.gaps || []).slice(0, 20).map(function (gap) {
        return "#" + gap.serial + " " + gap.title + ": " + gap.status + (gap.error ? " — " + gap.error : "");
      }).join("\\n");
    }).catch(function (error) {
      if (current.novel && current.novel.id === novelId) {
        els["novel-completeness"].textContent = "Couldn't check completeness: " + error.message;
      }
    });
  }
  // The library counts a novel missing only these as complete.
  function noteSkipped(novelId, count) {
    var mark = function (n) { if (n && n.id === novelId) { n.extra = n.extra || {}; n.extra.skipped = new Array(count).fill(0); } };
    (lib.everything || []).forEach(mark); (lib.all || []).forEach(mark); mark(current.novel);
  }
  els["skip-btn"].addEventListener("click", function () {
    if (!current.novel) return;
    var novelId = current.novel.id, btn = this;
    btn.disabled = true;
    api("/novel/" + novelId + "/skip-unavailable", { method: "POST" }).then(function (report) {
      noteSkipped(novelId, report.skipped || 0);
      return refreshCompleteness(novelId);
    }).then(function () {
      current.chapters = []; current.offset = current.listFrom || 0; els["chap-list"].innerHTML = ""; loadChapters();
    }).catch(function (e) { alert("Couldn't do without them: " + e.message); })
      .finally(function () { btn.disabled = false; });
  });
  els["unskip-link"].addEventListener("click", function () {
    if (!current.novel) return;
    var novelId = current.novel.id;
    api("/novel/" + novelId + "/skip-unavailable", { method: "DELETE" }).then(function () {
      noteSkipped(novelId, 0);
      return refreshCompleteness(novelId);
    }).then(function () {
      current.chapters = []; current.offset = current.listFrom || 0; els["chap-list"].innerHTML = ""; loadChapters();
    }).catch(function (e) { alert("Couldn't do that: " + e.message); });
  });
  // Why a chapter is not here, in a few words (its whole error as a tooltip).
  function whyMissing(ch) {
    var ex = ch.extra || {};
    var skipped = ((current.novel && current.novel.extra && current.novel.extra.skipped) || []).indexOf(ch.serial) >= 0;
    var at = ex.not_here && ex.not_here.at, host = "";
    if (at) { try { host = new URL(at).hostname.replace(/^www[.]/, ""); } catch (e) { host = ""; } }
    var why = at ? "only on " + (host || "another site")
      : ex.download_status === "empty" ? "the site gives an empty page"
      : ex.download_status === "failed" ? "failed: " + String(ex.download_error || "").split("\\n")[0].slice(0, 70)
      : "not downloaded";
    return { text: (skipped ? "done without · " : "") + why, title: ex.download_error || "", link: at || "" };
  }
  function pollRepair(novelId, jobId) {
    api("/job/" + jobId + "?light=true").then(function (job) {
      var active = job.status === 0 || job.status === 1 || job.status === 5 ||
        job.status === "PENDING" || job.status === "RUNNING" || job.status === "PAUSED";
      var visible = current.novel && current.novel.id === novelId;
      if (active) {
        if (visible) els["repair-status"].textContent = "Repairing chapters and rebuilding EPUB… " + job.done + "/" + job.total;
        later(function () { pollRepair(novelId, jobId); }, 3000);
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
      current.chapters = []; current.offset = 0; current.listFrom = 0; els["chap-list"].innerHTML = "";
      loadChapters();
    }).catch(function (e) {
      if (jobGone(e)) {
        // deleted (or the library moved): asking again would never end
        delete repairJobs[novelId];
        if (current.novel && current.novel.id === novelId) {
          els["repair-btn"].disabled = false;
          els["repair-status"].textContent = "The repair job is no longer there. Repair again to continue.";
        }
        return;
      }
      later(function () { pollRepair(novelId, jobId); }, 5000);
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
  els["getch-btn"].addEventListener("click", function () {
    if (!current.novel) return;
    var btn = this; btn.disabled = true;
    getChapters([current.novel], function (job) {
      if (!job) { btn.disabled = false; return; }
      btn.textContent = "⬇ Downloading chapters…";
    });
  });
  els["repair-download"].addEventListener("click", function () {
    if (!current.novel) return;
    var novelId = current.novel.id;
    var artifactId = repairedArtifacts[novelId];
    if (!artifactId) return;
    var btn = this; btn.disabled = true;
    api("/artifact/" + artifactId).then(function (artifact) {
      var fileUrl = "/static/" + artifact.output_file.split("/").map(encodeURIComponent).join("/") +
        "?token=" + encodeURIComponent(token());
      // An iPhone or iPad cannot save a file handed to it by the page (it sat on
      // a blank page, loading for ever): it is sent to the file itself, which
      // Safari offers to open in Books or save to Files.
      var apple = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
        (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
      if (apple) {
        if (current.novel && current.novel.id === novelId) {
          msg.textContent = "✓ " + artifact.file_name + " is ready: choose Open in Books, or Download. If nothing happens, open this page in Safari (the compass button) and try again.";
        }
        window.location.href = fileUrl;
        return;
      }
      return fetch(fileUrl, {
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
    els["novel-meta"].textContent = [novel.authors || "", (novel.chapter_count || 0) + " chapters", novel.domain || "",
      rankOf(novel) !== Infinity ? "#" + novel.extra.rank + " on " + (novel.domain || "its site") : ""]
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
        current.chapters = []; current.offset = 0; current.listFrom = 0; els["chap-list"].innerHTML = "";
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
    els["getch-btn"].classList.toggle("hidden", !detailsOnly(novel));
    els["getch-btn"].disabled = false;
    els["getch-btn"].textContent = "⬇ Download all chapters";
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
      // built off-document and added in one go: one layout for the batch, and
      // the reading position is read once instead of once per row
      var here = posOf(current.novel.id);
      var frag = document.createDocumentFragment();
      items.forEach(function (ch) {
        var row = document.createElement("div");
        row.className = "chap" + (ch.is_available ? "" : " unavail");
        var n = document.createElement("span"); n.className = "n"; n.textContent = "#" + ch.serial;
        var t = document.createElement("span"); t.className = "t"; t.textContent = ch.title || ("Chapter " + ch.serial);
        // read up to where you are: earlier chapters ticked, yours marked
        var rd = document.createElement("span"); rd.className = "rd";
        if (here && here.serial) {
          var done = ch.serial < here.serial || (ch.serial === here.serial && (here.scroll || 0) >= 0.9);
          if (done) { rd.textContent = "✓"; rd.title = "Read"; }
          if (ch.serial === here.serial) { row.className += " here"; row.title = "You are here"; if (!done) rd.textContent = "▶"; }
        }
        row.appendChild(rd); row.appendChild(n); row.appendChild(t);
        if (ch.is_available) row.addEventListener("click", function () { openChapter(ch.id); });
        else {
          gaps = true;
          var why = whyMissing(ch);
          var u = document.createElement(why.link ? "a" : "span"); u.className = "muted"; u.textContent = why.text;
          if (why.title) u.title = why.title;
          if (why.link) { u.href = why.link; u.target = "_blank"; u.rel = "noopener"; }
          row.appendChild(u);
        }
        frag.appendChild(row);
      });
      els["chap-list"].appendChild(frag);
      current.offset += items.length;
      var total = (res && res.total) || current.offset;
      els["chap-more-wrap"].classList.toggle("hidden", current.offset >= total);
      // a long list opens anywhere, not only from chapter 1
      current.total = total;
      els["chap-jump-wrap"].classList.toggle("hidden", total <= 100);
      els["chap-jump"].max = total;
      els["chap-jump-here"].classList.toggle("hidden", !(here && here.serial));
      els["chap-jump-start"].classList.toggle("hidden", !current.listFrom);
      // offer cross-source healing only when there are gaps to fill
      if (gaps) {
        els["heal-btn"].classList.remove("hidden");  // deep-btn stays hidden: this runs it
        maybeAutoHeal();
      }
    }).catch(function (e) { els["chap-list"].innerHTML = "<p class='muted'>Couldn't load chapters: " + e.message + "</p>"; });
  }
  // Show the chapter list from this position (1 = the first chapter) on.
  function listChaptersFrom(position) {
    var total = current.total || (current.novel && current.novel.chapter_count) || position;
    var at = Math.max(1, Math.min(position, total));
    current.listFrom = Math.max(0, at - 6);  // a few before it, for context
    current.offset = current.listFrom;
    current.chapters = [];
    els["chap-list"].innerHTML = "";
    loadChapters();
    els["chap-list"].scrollIntoView({ block: "start" });
  }
  els["chap-jump-go"].addEventListener("click", function () {
    var n = parseInt(els["chap-jump"].value, 10);
    if (n > 0) listChaptersFrom(n);
  });
  els["chap-jump"].addEventListener("keydown", function (e) {
    if (e.key === "Enter") els["chap-jump-go"].click();
  });
  els["chap-jump-here"].addEventListener("click", function () {
    var here = current.novel && posOf(current.novel.id);
    if (here && here.serial) listChaptersFrom(here.serial);
  });
  els["chap-jump-start"].addEventListener("click", function () { listChaptersFrom(1); });

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
      current.chapters = []; current.offset = 0; current.listFrom = 0; els["chap-list"].innerHTML = "";
      loadChapters();
    }).catch(function () { /* auto-heal is best-effort */ });
  }

  // One button fills the gaps: the job behind it tries a copy in the library,
  // then the novel's own site, then other sites, and needs no second copy.
  els["heal-btn"].addEventListener("click", function () { els["deep-btn"].click(); });

  // Deep heal: a background job searches other sites for the missing chapters,
  // downloads them, and copies matches in. Poll it and refresh when done.
  els["deep-btn"].addEventListener("click", function () {
    if (!current.novel) return;
    var btn = this; btn.disabled = true;
    var novelId = current.novel.id;
    els["heal-btn"].disabled = true;
    els["heal-msg"].textContent = "Looking for the missing chapters…";
    els["heal-msg"].classList.remove("hidden");
    api("/novel/" + novelId + "/heal-deep", { method: "POST" }).then(function (job) {
      (function poll() {
        api("/job/" + job.id + "?light=true").then(function (j) {
          if (!current.novel || current.novel.id !== novelId) { btn.disabled = false; els["heal-btn"].disabled = false; return; }
          var ex = j.extra || {};
          var active = j.status === 0 || j.status === 1 || j.status === "PENDING" || j.status === "RUNNING";
          if (active) {
            if (ex.source) {
              els["heal-msg"].textContent = (ex.phase === "searching" ? "Searching " : "Downloading from ") +
                ex.source + "… (" + (j.done || 0) + "/" + (j.total || 0) + " filled)";
            }
            later(poll, 3000);
            return;
          }
          btn.disabled = false; els["heal-btn"].disabled = false;
          els["heal-msg"].textContent = ex.message || (j.error ? ("Failed: " + j.error) : "Done.");
          if (ex.healed) {
            current.chapters = []; current.offset = 0; current.listFrom = 0; els["chap-list"].innerHTML = "";
            loadChapters();
          }
        }).catch(function () { later(poll, 5000); });
      })();
    }).catch(function (e) {
      btn.disabled = false; els["heal-btn"].disabled = false;
      els["heal-msg"].textContent = "Couldn't start: " + e.message;
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
  var opening = 0;
  function openChapter(chapterId, scrollTo) {
    var mine = ++opening;  // a page-turn pressed again before this one came back wins
    show("view-reader");
    els["reader-title"].textContent = "Loading…";
    els["reader-content"].innerHTML = "";
    els["reader-pos"].textContent = "";
    hideTransPop();
    window.scrollTo(0, 0);
    readChapter(chapterId).then(function (res) {
      if (mine !== opening) return;
      var ch = res.chapter || {};
      var novel = res.novel || current.novel || {};
      current.novel = novel;
      els["reader-title"].textContent = ch.title || ("Chapter " + ch.serial);
      els["reader-content"].innerHTML = res.content || "<p class='muted'>No content available for this chapter.</p>";
      els["reader-pos"].textContent = "#" + (ch.serial || "");
      current.chapterId = chapterId;  // before the pages load: they stop if the chapter changes
      var imgCount = hydrateImages(els["reader-content"], chapterId);
      els["fit-btn"].classList.toggle("hidden", imgCount < 2);
      applyFit();
      savePos(novel.id, chapterId, ch.serial);
      addStat(1, 0);
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
      if (res.next_id) preloadNext(res.next_id);
      els["prev-btn"].disabled = !res.previous_id;
      els["next-btn"].disabled = !res.next_id;
      els["prev-btn"].onclick = function () { if (res.previous_id) openChapter(res.previous_id); };
      els["next-btn"].onclick = function () { if (res.next_id) openChapter(res.next_id); };
    }).catch(function (e) {
      if (mine !== opening) return;
      els["reader-title"].textContent = "Error";
      els["reader-content"].innerHTML = "<p class='muted'>Couldn't load chapter: " + e.message + "</p>";
    });
  }

  // Manga images are stored as <img src="images/<id>.jpg">; fetch each through
  // the authenticated image endpoint (via the shared blob cache, so preloaded
  // chapters render instantly) and swap in an object URL. Returns image count.
  function hydrateImages(root, chapterId) {
    // in page order, four at a time: all of them at once made the first pages
    // wait behind the last, and each page that arrived pushed the rest down
    var todo = [];
    root.querySelectorAll("img[src]").forEach(function (img) {
      var src = img.getAttribute("src") || "";
      var m = src.match(/images\\/([0-9a-fA-F]+)\\.jpg/);
      if (!m) return;
      img.decoding = "async";
      img.removeAttribute("src");
      img.alt = "";  // not the page's id while it loads
      img.classList.add("pending");
      todo.push({ img: img, id: m[1] });
    });
    var next = 0, shown = chapterId;
    function one() {
      if (next >= todo.length || current.chapterId !== shown) return;
      var item = todo[next++];
      cacheImage(item.id, chapterId)
        .then(function (u) {
          item.img.onload = function () { item.img.classList.remove("pending"); };
          item.img.src = u;
        })
        .catch(function () { item.img.classList.remove("pending"); item.img.alt = "(image unavailable)"; })
        .then(one);
    }
    for (var i = 0; i < 4; i++) one();
    return todo.length;
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
    els["reader-content"].classList.toggle("f-serif", prefs.font !== "system");
    els["reader-content"].classList.toggle("sp-compact", prefs.spacing === "compact");
    els["reader-content"].classList.toggle("sp-airy", prefs.spacing === "airy");
    els["view-reader"].style.maxWidth = prefs.width === "narrow" ? "600px" : (prefs.width === "wide" ? "980px" : "720px");
    els["lib-nonsfw"].checked = hideNSFW();  // set on another device, synced here
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
    chipRow(els["aa-font"], [{v:"serif",t:"Book"},{v:"system",t:"Plain"}], "font", "serif");
    chipRow(els["aa-width"], [{v:"narrow",t:"Narrow"},{v:"normal",t:"Normal"},{v:"wide",t:"Wide"}], "width", "normal");
    chipRow(els["aa-trans"], [{v:"",t:"Off"},{v:"on",t:"On"}], "translate", "");
    chipRow(els["aa-spacing"], [{v:"compact",t:"Compact"},{v:"normal",t:"Normal"},{v:"airy",t:"Airy"}], "spacing", "normal");
    chipRow(els["aa-fit"], [{v:"width",t:"Fit width"},{v:"height",t:"Fit height"},{v:"strip",t:"Strip"}], "fit", "width");
    chipRow(els["aa-tap"], [{v:"",t:"Off"},{v:"on",t:"On"}], "tapturn", "");
  }
  els["aa-btn"].addEventListener("click", function (e) {
    e.stopPropagation();
    renderAaPanel();
    els["aa-panel"].classList.toggle("hidden");
  });
  document.addEventListener("click", function (e) {
    // a chip picked in the panel is drawn again, so it is no longer in the page
    // by the time this runs: that is still a click inside the panel
    if (!els["aa-panel"].classList.contains("hidden") && e.target.isConnected &&
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
  function cacheImage(id, chapterId) {
    if (imgCache[id]) return Promise.resolve(imgCache[id]);
    return fetch("/api/chapter/image/" + id + (chapterId ? "?chapter=" + encodeURIComponent(chapterId) : ""), { headers: { "Authorization": "Bearer " + token() } })
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
  // The preloaded chapter is kept (briefly) so opening it doesn't fetch it again.
  var preloaded = null;
  function readChapter(chapterId) {
    var hit = preloaded;
    preloaded = null;
    if (hit && hit.id === chapterId && Date.now() - hit.at < 300000) {
      return hit.req.then(function (res) {
        // fetched ahead without counting as read: it is read now
        api("/read-history/add/" + chapterId, { method: "POST" }).catch(function () {});
        return res;
      }, function () { return api("/chapter/" + chapterId + "/read?auto_fetch=false"); });
    }
    return api("/chapter/" + chapterId + "/read?auto_fetch=false");
  }
  // The next chapter is fetched while this one is read, so turning the page
  // shows it at once: the text, and a manga chapter's pages.
  function preloadNext(chapterId) {
    var req = api("/chapter/" + chapterId + "/read?auto_fetch=false&preload=true");
    preloaded = { id: chapterId, at: Date.now(), req: req };
    req.then(function (res) {
      var ids = imageIdsIn(res && res.content);
      var i = 0;
      (function step() {
        if (i >= ids.length) return;
        cacheImage(ids[i++], chapterId).then(step).catch(step);
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
      if (hideNSFW() && byId[novelId] && isNSFW(byId[novelId])) return;
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
    if (e.key === "Escape") { els["aa-panel"].classList.add("hidden"); document.body.classList.remove("bars-off"); }
    if (e.key === "ArrowRight" && !els["next-btn"].disabled) els["next-btn"].click();
    if (e.key === "ArrowLeft" && !els["prev-btn"].disabled) els["prev-btn"].click();
  });

  // A tap on the page brings the bars back (or tucks them away); with Tap to
  // turn on, a tap near either edge scrolls a screen, and past the end opens
  // the next chapter. Left alone while tap-translate is on: it uses the taps.
  // (anywhere on the page, not only the text column: on a wide screen the
  // edges are beside it)
  document.addEventListener("click", function (e) {
    if (!document.body.classList.contains("reading") || prefs.translate === "on") return;
    if (!e.target.isConnected || (e.target.closest &&
        e.target.closest("a, button, input, select, .topbar, .reader-nav, #aa-panel, #trans-pop, #reader-title"))) return;
    var sel = window.getSelection && window.getSelection();
    if (sel && !sel.isCollapsed) return;
    var x = e.clientX / window.innerWidth;
    if (prefs.tapturn === "on" && (x < 0.3 || x > 0.7)) {
      var room = document.documentElement.scrollHeight - window.innerHeight;
      if (x > 0.7 && window.scrollY >= room - 4) {
        if (!els["next-btn"].disabled) els["next-btn"].click();
        return;
      }
      window.scrollBy({ top: (x > 0.7 ? 1 : -1) * window.innerHeight * 0.85, behavior: "smooth" });
      return;
    }
    // a finger's tap toggles the bars; a mouse click on the text does not
    if (lastPointer === "touch") document.body.classList.toggle("bars-off");
  });
  var lastPointer = "";
  document.addEventListener("pointerdown", function (e) { lastPointer = e.pointerType || ""; }, { passive: true });
  // with a mouse, the bars come back when it goes to the top or bottom edge
  document.addEventListener("mousemove", function (e) {
    if (document.body.classList.contains("bars-off") &&
        (e.clientY < 70 || e.clientY > window.innerHeight - 90)) document.body.classList.remove("bars-off");
  }, { passive: true });

  // remember the in-chapter position while reading (throttled)
  var scrollTimer = null;
  var lastY = 0;
  window.addEventListener("scroll", function () {
    if (els["view-reader"].classList.contains("hidden")) return;
    var y = window.scrollY, room = document.documentElement.scrollHeight - window.innerHeight;
    els["read-progress"].style.width = (room > 0 ? Math.min(100, 100 * y / room) : 100) + "%";
    var panelOpen = !els["aa-panel"].classList.contains("hidden");
    if (room - y < 90 || y < 60 || panelOpen) document.body.classList.remove("bars-off");  // the top, or Next in reach
    else if (y > lastY + 14) document.body.classList.add("bars-off");
    else if (y < lastY - 14) document.body.classList.remove("bars-off");
    if (Math.abs(y - lastY) > 14) lastY = y;
    if (!current.novel || !current.chapterId) return;
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(function () {
      var max = document.documentElement.scrollHeight - window.innerHeight;
      var frac = max > 0 ? Math.min(1, Math.max(0, window.scrollY / max)) : 0;
      saveScroll(current.novel.id, current.chapterId, frac);
    }, 400);
  }, { passive: true });

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
    if (localStorage.getItem(FILTERS_OPEN)) els["lib-filters"].classList.remove("hidden");
    renderFiltersButton();
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
// Copies kept on the way past, apart from what "Save for offline" keeps in
// CACHE, so the oldest can be let go without losing a chapter saved on purpose.
const AUTO = "lncrawl-reader-auto-v1";
const AUTO_MAX = 2000;
// Answers that change by the second and are asked for over and over (job
// progress, the unfinished-novels count, settings, admin pages): a disk write
// on every poll, kept for good, and of no use offline.
const NOT_KEPT = ["/api/job", "/api/novels/unfinished", "/api/settings/", "/api/admin/", "/api/auth/"];
const SHELL = ["/reader", "/reader/manifest.webmanifest", "/reader/icon.svg"];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).catch(function () {}));
  self.skipWaiting();
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE && k !== AUTO; }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

// Network-first, fall back to cache: fresh when online, readable when offline.
self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  var keep = !NOT_KEPT.some(function (prefix) { return url.pathname.indexOf(prefix) === 0; });
  e.respondWith(
    fetch(req).then(function (res) {
      if (keep && res && res.ok) {
        var copy = res.clone();
        // a chapter fetched ahead (preload=true) is kept as the chapter itself,
        // the address it is asked for by when opened again with no connection
        var key = new URL(url.href);
        key.searchParams.delete("preload");
        caches.open(AUTO).then(function (c) { return c.put(key.href, copy).then(function () { trim(c); }); }).catch(function () {});
      }
      return res;
    }).catch(function () {
      return caches.match(req).then(function (hit) {
        return hit || (req.mode === "navigate" ? caches.match("/reader") : undefined);
      });
    })
  );
});

// Oldest first out once there are more than AUTO_MAX, looked at every 50 copies.
var sinceTrim = 0;
function trim(c) {
  if (++sinceTrim < 50) return;
  sinceTrim = 0;
  c.keys().then(function (keys) {
    return Promise.all(keys.slice(0, Math.max(0, keys.length - AUTO_MAX)).map(function (k) { return c.delete(k); }));
  }).catch(function () {});
}
"""

_MANIFEST = """{
  "name": "LNCrawl Reader",
  "short_name": "LNCrawl",
  "description": "Read your downloaded light-novel and manga library, online or offline.",
  "start_url": "/reader",
  "scope": "/reader",
  "display": "standalone",
  "orientation": "portrait",
  "background_color": "#16141d",
  "theme_color": "#16141d",
  "icons": [
    { "src": "/reader/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable" }
  ]
}"""

_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
  <rect width="192" height="192" rx="36" fill="#16141d"/>
  <rect x="48" y="40" width="96" height="112" rx="8" fill="#f2b84b"/>
  <rect x="64" y="40" width="16" height="112" fill="#d99a26"/>
  <rect x="92" y="64" width="40" height="8" rx="4" fill="#cbd2dd"/>
  <rect x="92" y="84" width="40" height="8" rx="4" fill="#cbd2dd"/>
  <rect x="92" y="104" width="28" height="8" rx="4" fill="#cbd2dd"/>
</svg>"""


# The pages are constants: each is encoded and gzipped once, and a browser
# revalidating its copy (``no-cache`` + ETag) gets a bodiless 304 back.
_HTML = "text/html; charset=utf-8"
_REVALIDATE = {"Cache-Control": "no-cache"}
_DAILY = {"Cache-Control": "public, max-age=86400"}


@router.get("/reader", include_in_schema=False)
async def reader_page(request: Request) -> Response:
    return blob_response(request, text_blob(_READER_HTML, _HTML), _REVALIDATE)


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
async def reader_manifest(request: Request) -> Response:
    return blob_response(request, text_blob(_MANIFEST, "application/manifest+json"), _DAILY)


@router.get("/reader/icon.svg", include_in_schema=False)
async def reader_icon(request: Request) -> Response:
    return blob_response(request, text_blob(_ICON_SVG, "image/svg+xml"), _DAILY)
