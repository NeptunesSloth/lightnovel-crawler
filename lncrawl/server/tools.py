"""Standalone "Tools" page.

A self-contained, dependency-free admin/power-user page that exposes a few
job actions directly over the REST API. It is intentionally kept outside of
``lncrawl/server/web`` (which is overwritten by the web-sync workflow from the
``lncrawl-web`` artifacts) so it survives front-end rebuilds. The markup is
embedded as a string constant so it is always packaged with the wheel/exe.

Served at ``GET /tools``.
"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .caching import blob_response, text_blob

router = APIRouter()

# NOTE: plain string (not an f-string) on purpose — the embedded CSS/JS is full
# of ``{}`` braces. There is nothing to interpolate here.
_TOOLS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>LNCrawl · Tools</title>
<style>
  /* A reading room at night: ink walls, paper text, one lamp. The lamp colour
     is kept for the thing to press next; nothing else on a screen uses it. */
  :root {
    color-scheme: dark;
    --ink: #16141d; --shelf: #1d1a27; --panel: #221f2e; --raised: #2d2939;
    --line: #373248; --paper: #efe9dc; --dim: #a59fb3; --faint: #7d778c;
    --lamp: #f2b84b; --lamp-deep: #d99a26; --leaf: #7fd1a5; --rose: #ff8f84; --sky: #9cc8ff;
    --serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
    --sans: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, Roboto, sans-serif;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; font-family: var(--sans); font-size: 15px; line-height: 1.55;
    background: var(--ink); color: var(--paper);
  }
  .shell { display: grid; grid-template-columns: 248px minmax(0, 1fr); min-height: 100%; }

  /* the menu */
  .menu {
    background: var(--shelf); border-right: 1px solid var(--line);
    padding: 22px 14px 16px; display: flex; flex-direction: column; gap: 2px;
    position: sticky; top: 0; height: 100vh; overflow-y: auto;
  }
  .brand { font-family: var(--serif); font-size: 21px; padding: 0 10px 2px; letter-spacing: .01em; }
  .brand-sub { color: var(--faint); font-size: 12.5px; padding: 0 10px 16px; }
  .menu button.go {
    display: block; width: 100%; margin: 0; text-align: left; background: transparent;
    color: var(--dim); font-weight: 500; font-size: 15px; padding: 10px 12px;
    border-radius: 8px; border-left: 3px solid transparent;
  }
  .menu button.go small { display: block; font-weight: 400; font-size: 12.5px; color: var(--faint); }
  .menu button.go:hover { background: var(--panel); color: var(--paper); }
  .menu button.go[aria-current="page"] {
    background: var(--raised); color: var(--paper); border-left-color: var(--lamp);
  }
  .menu button.go .dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: var(--lamp); margin-left: 6px; vertical-align: middle;
  }
  .menu .rule { height: 1px; background: var(--line); margin: 12px 8px; }
  .menu a.out {
    display: block; padding: 8px 12px; border-radius: 8px; color: var(--dim);
    text-decoration: none; font-size: 14px;
  }
  .menu a.out:hover { background: var(--panel); color: var(--paper); }
  .menu .account { margin-top: auto; padding: 12px 10px 0; font-size: 13px; color: var(--faint); }
  .menu .account button { margin-top: 8px; padding: 6px 12px; font-size: 13px; }

  /* the page */
  .page { padding: 34px 40px 28px; max-width: 880px; width: 100%; }
  .pane { display: none; }
  .pane.shown { display: block; }
  body.signed-out .pane { display: none; }
  body.signed-out #auth-card { display: block; }
  body.signed-out .menu button.go { opacity: .4; pointer-events: none; }
  .pane > h2, .pane > details > summary.title {
    font-family: var(--serif); font-weight: 500; font-size: 30px; line-height: 1.2;
    margin: 0 0 8px; letter-spacing: .005em;
  }
  h3 { font-size: 16px; font-weight: 600; margin: 30px 0 4px; }
  p.hint { color: var(--dim); font-size: 14px; margin: 0 0 16px; max-width: 68ch; }
  .pane > p.hint:first-of-type { font-size: 15px; margin-bottom: 22px; }
  details { margin-top: 18px; border-top: 1px solid var(--line); padding-top: 10px; }
  details > summary {
    cursor: pointer; color: var(--dim); font-size: 14px; list-style: none;
    user-select: none; padding: 4px 0;
  }
  details > summary::-webkit-details-marker { display: none; }
  details > summary::before { content: "+ "; color: var(--faint); }
  details[open] > summary::before { content: "\\2212  "; }
  details > summary:hover { color: var(--paper); }
  label { display: block; font-size: 14px; color: var(--dim); margin: 14px 0 6px; }
  input[type=text], input[type=email], input[type=password], select {
    width: 100%; padding: 11px 13px; border-radius: 8px;
    border: 1px solid var(--line); background: var(--ink); color: var(--paper);
    font: inherit;
  }
  input::placeholder { color: var(--faint); }
  input[type=checkbox] { width: 17px; height: 17px; accent-color: var(--lamp); flex: 0 0 auto; }
  :focus-visible { outline: 2px solid var(--lamp); outline-offset: 2px; }
  code { background: var(--ink); padding: 1px 5px; border-radius: 5px; font-size: 13px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .row > div { min-width: 120px; }
  .row.checkbox { margin-top: 12px; color: var(--paper); flex-wrap: nowrap; align-items: flex-start; }
  .row.checkbox label { color: var(--paper); }
  button {
    margin-top: 16px; padding: 11px 20px; border: 0; border-radius: 8px;
    background: var(--lamp); color: #201803; font: inherit; font-weight: 650; cursor: pointer;
  }
  button:hover { background: var(--lamp-deep); }
  button.secondary { background: var(--raised); color: var(--paper); font-weight: 500; }
  button.secondary:hover { background: var(--line); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  button.preset { margin-top: 0; padding: 7px 12px; font-size: 13px; }
  button.preset.active { background: var(--lamp); color: #201803; }
  .row > input[type=text] { flex: 1 1 220px; width: auto; }
  .row > button { flex: 0 0 auto; }
  #library-list { font-size: 14px; }
  #drop-zone {
    border: 2px dashed var(--line); border-radius: 12px; padding: 44px 16px;
    text-align: center; cursor: pointer; transition: border-color .15s, background .15s;
  }
  #drop-zone:hover { border-color: var(--faint); }
  #drop-zone.dragging { border-color: var(--lamp); background: var(--panel); }
  #drop-zone strong { display: block; margin-bottom: 4px; font-family: var(--serif); font-size: 20px; font-weight: 500; }
  #import-status { margin-top: 10px; font-size: 14px; min-height: 18px; }
  .libitem {
    display: flex; justify-content: space-between; gap: 10px; align-items: center;
    padding: 10px 2px; border-bottom: 1px solid var(--line);
  }
  .libitem a { color: var(--sky); text-decoration: none; cursor: pointer; }
  .libitem a:hover { text-decoration: underline; }
  .muted { color: var(--dim); font-size: 14px; }

  /* where the library is, and where it could go */
  .where {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 16px 18px; margin-bottom: 8px;
  }
  .where p.hint { margin: 0; color: var(--paper); font-size: 15px; }
  .place {
    display: flex; align-items: center; gap: 14px; padding: 13px 4px;
    border-bottom: 1px solid var(--line);
  }
  .place:last-child { border-bottom: 0; }
  .place .name { font-weight: 600; word-break: break-all; }
  .place .about { color: var(--dim); font-size: 13.5px; }
  .place .grow { flex: 1 1 auto; min-width: 0; }
  .place button { margin: 0; flex: 0 0 auto; }
  .place .bar { height: 5px; border-radius: 3px; background: var(--raised); margin-top: 7px; max-width: 320px; }
  .place .bar i { display: block; height: 100%; border-radius: 3px; background: var(--faint); }
  .notice {
    border: 1px solid var(--lamp); border-radius: 12px; padding: 14px 18px; margin-bottom: 22px;
    display: flex; gap: 14px; align-items: center; background: rgba(242, 184, 75, .07);
  }
  .notice .grow { flex: 1 1 auto; }
  .notice button { margin: 0; flex: 0 0 auto; }

  /* picking a folder */
  .picker-row { flex-wrap: nowrap; }
  .picker-row > input[type=text] { flex: 1 1 auto; min-width: 0; }
  .picker-row > button { margin-top: 0; }
  dialog#folder-picker {
    border: 1px solid var(--line); border-radius: 14px; padding: 0; width: min(640px, calc(100vw - 32px));
    background: var(--panel); color: var(--paper);
  }
  dialog#folder-picker::backdrop { background: rgba(10, 8, 16, .66); }
  .picker { padding: 20px 22px 18px; display: flex; flex-direction: column; gap: 10px; }
  .picker h2 { font-family: var(--serif); font-weight: 500; font-size: 24px; margin: 0; }
  .trail { display: flex; flex-wrap: wrap; align-items: center; gap: 2px; font-size: 14px; }
  .trail button {
    margin: 0; padding: 4px 8px; background: transparent; color: var(--sky); font-weight: 500; border-radius: 6px;
  }
  .trail button:hover { background: var(--raised); }
  .trail button[aria-current] { color: var(--paper); }
  .trail .sep { color: var(--faint); }
  .picker-note { min-height: 20px; margin: 0; }
  .picker-list {
    border: 1px solid var(--line); border-radius: 10px; background: var(--ink);
    height: min(52vh, 400px); overflow-y: auto;
  }
  .picker-list button {
    display: flex; width: 100%; align-items: center; gap: 12px; margin: 0; text-align: left;
    background: transparent; color: var(--paper); font-weight: 400; border-radius: 0;
    padding: 10px 14px; border-bottom: 1px solid var(--shelf);
  }
  .picker-list button:hover, .picker-list button:focus-visible { background: var(--raised); outline-offset: -2px; }
  .picker-list .glyph { width: 18px; color: var(--lamp); flex: 0 0 auto; text-align: center; }
  .picker-list .label { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .picker-list .meta { color: var(--faint); font-size: 13px; flex: 0 0 auto; }
  .picker-list .tag {
    color: var(--leaf); background: rgba(127, 209, 165, .14); border-radius: 999px;
    padding: 1px 9px; font-size: 12.5px; flex: 0 0 auto;
  }
  .picker-list .empty { padding: 18px 14px; color: var(--faint); }
  .picker-actions { flex-wrap: nowrap; }
  .picker-actions .grow { flex: 1 1 auto; }
  .picker-actions button { margin-top: 4px; }
  @media (max-width: 520px) {
    .picker-actions { flex-wrap: wrap; }
    .picker-actions .grow { display: none; }
  }

  /* what has happened */
  .activity { margin-top: 36px; border-top: 1px solid var(--line); padding-top: 16px; }
  .activity h2 { font-size: 14px; font-weight: 600; color: var(--dim); margin: 0 0 8px; font-family: var(--sans); }
  #log {
    background: var(--shelf); border: 1px solid var(--line); border-radius: 10px;
    padding: 12px 14px; font-family: ui-monospace, "Cascadia Mono", Consolas, Menlo, monospace;
    font-size: 13px; white-space: pre-wrap; word-break: break-word;
    max-height: 260px; overflow: auto;
  }
  .log-ok { color: var(--leaf); }
  .log-err { color: var(--rose); }
  .log-info { color: var(--sky); }
  .pill { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12.5px; }
  .pill.on { background: rgba(127, 209, 165, .14); color: var(--leaf); }
  .pill.off { background: rgba(255, 143, 132, .14); color: var(--rose); }
  .hidden { display: none !important; }
  a { color: var(--sky); }
  .build { color: var(--faint); font-size: 12.5px; margin: 18px 0 0; }

  @media (max-width: 820px) {
    .shell { display: block; }
    .menu {
      position: sticky; top: 0; z-index: 5; height: auto; flex-direction: row; align-items: center;
      overflow-x: auto; padding: 8px 10px; gap: 4px; border-right: 0; border-bottom: 1px solid var(--line);
    }
    .brand, .brand-sub, .menu .rule, .menu .account, .menu button.go small { display: none; }
    .menu button.go {
      width: auto; white-space: nowrap; border-left: 0; border-bottom: 3px solid transparent;
      padding: 9px 12px; flex: 0 0 auto;
    }
    .menu button.go[aria-current="page"] { border-bottom-color: var(--lamp); }
    .menu a.out { white-space: nowrap; flex: 0 0 auto; }
    .page { padding: 22px 16px 24px; }
    .pane > h2 { font-size: 25px; }
    .place, .notice { flex-wrap: wrap; }
  }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<div class="shell">
<nav class="menu" aria-label="Tools">
  <div class="brand">Lightnovel Crawler</div>
  <div class="brand-sub">Tools</div>
  <button class="go secondary" data-go="download">Download a site<small>Every novel on it, unattended</small></button>
  <button class="go secondary" data-go="browse">Browse a site<small>List its novels, pick later</small></button>
  <button class="go secondary" data-go="import">Import books<small>From a ZIP or e-book files</small></button>
  <button class="go secondary" data-go="zip">Make a ZIP<small>Of the books you already have</small></button>
  <button class="go secondary" data-go="library">Library and drives<span id="library-dot" class="dot hidden"></span><small>Where it is, moving it, backups</small></button>
  <button class="go secondary" data-go="repair">Repair a novel<small>Fill in missing chapters</small></button>
  <button class="go secondary" data-go="alerts">Alerts<small>Tell me when a download ends</small></button>
  <div class="rule"></div>
  <a id="reader-link" href="/reader" target="_blank" class="out" title="Opens in its own window so this page keeps watching your download">Open the Reader</a>
  <a href="/downloads" target="_blank" class="out" title="Every download in one place: progress, pause, resume, stop">All downloads</a>
  <a id="back-link" href="/" class="out">Back to the app</a>
  <div class="account">
    <div id="logged-in" class="hidden">
      Signed in as <span id="who" class="pill on"></span><br />
      <button id="logout-btn" class="secondary">Sign out</button>
    </div>
  </div>
</nav>
<main class="page">
  <div id="space-notice" class="notice hidden">
    <div class="grow"><b id="space-title"></b>
      <div class="muted">Downloads stop when it runs out, and at nothing free the app stops
        answering. There is still room to move the library to a bigger drive.</div></div>
    <button id="space-go">Move the library</button>
  </div>
  <div id="found-notice" class="notice hidden">
    <div class="grow"><b id="found-title"></b><div class="muted" id="found-text"></div></div>
    <button id="found-use">Use that library</button>
  </div>

  <section class="pane" id="auth-card">
    <h2>Sign in</h2>
    <p class="hint">Use the same email and password as in the app.</p>
    <div id="logged-out">
      <label for="email">Email or username</label>
      <input id="email" autocapitalize="none" autocorrect="off" spellcheck="false" type="text" autocomplete="username" />
      <label for="password">Password</label>
      <input id="password" autocapitalize="none" autocorrect="off" spellcheck="false" type="password" autocomplete="current-password" />
      <button id="login-btn">Sign in</button>
    </div>
  </section>

  <section class="pane" data-pane="download">
    <h2>Download one novel</h2>
    <p class="hint">Paste a novel's link (its page on the site, not the site's home page) and
      press Download. Only that novel is downloaded, every chapter, into your library. Paste
      several links, one per line, to download a few at once.</p>
    <label for="one-urls">Novel link(s)</label>
    <textarea id="one-urls" rows="3" placeholder="https://novelsource.example/book/the-novel"></textarea>
    <button id="one-start">Download</button>

    <h2 style="margin-top:28px">Download a whole site</h2>
    <p class="hint">Paste a site's address and press Start. Every novel on it is found and
      downloaded — manga too. It tunes its speed to the site, waits out blocks and hiccups and
      tries again, carries on after the app or computer restarts, and keeps the computer awake
      while it works, so you can start it and leave it for days. You get a notification when
      it's done.</p>
    <label for="exp-url">Site address</label>
    <input id="exp-url" type="text" placeholder="https://novelsource.example/" />
    <label for="exp-make" style="margin-top:12px">What to make</label>
    <select id="exp-make">
      <option value="library" selected>Add to my library — read it in the Reader</option>
      <option value="zip">Also make e-books, bundled into one ZIP to keep or move</option>
      <option value="ziponly">Only the ZIP of e-books — nothing is kept in my library</option>
    </select>
    <div class="row checkbox">
      <input id="exp-untildone" type="checkbox" checked />
      <label for="exp-untildone" style="margin:0">Keep going until everything is downloaded (recommended — safe to leave for days)</label>
    </div>
    <div class="hidden" id="exp-zip-opts">
    <div class="row">
      <div style="flex:2">
        <label for="exp-format">E-book format</label>
        <select id="exp-format">
          <option value="epub" selected>EPUB (best for manga &amp; most readers)</option>
          <option value="cbz">CBZ (manga only — comic reader archive)</option>
          <option value="pdf">PDF</option>
          <option value="azw3">AZW3 (Kindle)</option>
          <option value="mobi">MOBI (older Kindle)</option>
          <option value="txt">TXT (text only)</option>
        </select>
      </div>
      <div style="flex:3">
        <label for="exp-savedir">Save the ZIP to folder</label>
        <div class="row picker-row">
          <input id="exp-savedir" type="text" placeholder="blank = Desktop, e.g. D:\\Novels" />
          <button type="button" class="secondary browse" data-for="exp-savedir">Browse…</button>
        </div>
      </div>
    </div>
    <div class="row checkbox">
      <input id="exp-completeonly" type="checkbox" />
      <label for="exp-completeonly" id="exp-completeonly-label" style="margin:0">Only fully downloaded novels in the ZIP (partly downloaded ones stay in the library)</label>
    </div>
    <p class="hint hidden" id="exp-ziponly-hint">Each novel is downloaded, made into an e-book for the
      ZIP and then taken out of the library again, so it never shows in the Reader. Novels that
      were in your library before stay there. The downloads still need room on the library's
      drive while they run; if you stop part way, what is unfinished stays until you resume.</p>
    </div>
    <details>
      <summary>Advanced options — you don't need these</summary>
      <label style="margin-top:12px">Speed</label>
      <div class="row" id="exp-presets">
        <button type="button" class="preset secondary" data-preset="fast">⚡ Fast</button>
        <button type="button" class="preset secondary" data-preset="balanced">⚖️ Balanced</button>
        <button type="button" class="preset secondary" data-preset="gentle">🐢 Gentle</button>
        <span class="muted" id="preset-note" style="align-self:center">Balanced — good default</span>
      </div>
      <div class="row">
        <div style="flex:1">
          <label for="exp-limit">Only the first N novels</label>
          <input id="exp-limit" type="text" inputmode="numeric" placeholder="blank = all" />
        </div>
        <div style="flex:1">
          <label for="exp-retries">Auto-retry rounds</label>
          <input id="exp-retries" type="text" inputmode="numeric" value="3" placeholder="3" />
        </div>
        <div style="flex:1">
          <label for="exp-discovery">Max discovery (min)</label>
          <input id="exp-discovery" type="text" inputmode="numeric" value="10" placeholder="10" />
        </div>
        <div style="flex:1">
          <label for="exp-maxch">Max chapters/novel</label>
          <input id="exp-maxch" type="text" inputmode="numeric" placeholder="blank = all" />
        </div>
        <div style="flex:1">
          <label for="exp-rps">Requests/sec</label>
          <input id="exp-rps" type="text" inputmode="decimal" placeholder="polite = 1" />
        </div>
        <div style="flex:1">
          <label for="exp-lanes">Novels at once</label>
          <select id="exp-lanes"><option>1</option><option selected>2</option><option>3</option><option>4</option></select>
        </div>
      </div>
      <div class="row checkbox">
        <input id="exp-polite" type="checkbox" />
        <label for="exp-polite" style="margin:0">Polite mode — pace requests to ~1/sec (fixes "ch 0/N" on protected sites)</label>
      </div>
      <div class="row checkbox">
        <input id="exp-dedupe" type="checkbox" checked />
        <label for="exp-dedupe" style="margin:0">Skip novels I already have from another source</label>
      </div>
      <div class="row checkbox">
        <input id="exp-resume" type="checkbox" checked />
        <label for="exp-resume" style="margin:0">Resume — reuse novels already finished in a previous run</label>
      </div>
      <div class="row checkbox">
        <input id="exp-update" type="checkbox" />
        <label for="exp-update" style="margin:0">Update only — re-check finished novels for new chapters and grab just those</label>
      </div>
      <div class="row checkbox">
        <input id="exp-autotune" type="checkbox" checked />
        <label for="exp-autotune" style="margin:0">Auto-tune rate — slow down when blocked, speed up when clear</label>
      </div>
      <div class="row checkbox">
        <input id="exp-sitemap" type="checkbox" checked />
        <label for="exp-sitemap" style="margin:0">Use the site's sitemap — finds the whole catalogue instead of the one page of results a search returns</label>
      </div>
      <p class="hint" style="margin-top:10px">Defaults work for most sites. If a site blocks the run,
        it auto-slows and (when a browser is available) solves the Cloudflare challenge by itself; turn
        on Polite mode or lower Requests/sec to 0.5/0.25 for stubborn ones.</p>
      <p class="hint">Requests/sec is what sets the speed, and it is a cap: the run never asks faster,
        and slows itself down further if the site pushes back. Because one request is in flight at a
        time while a cap is set, a value above 1&ndash;2 is limited by how fast the site answers &mdash;
        leave it blank (the &ldquo;Fast&rdquo; preset) to download several at once instead.</p>
    </details>
    <div class="row" style="margin-top:14px">
      <button id="export-btn">Start</button>
      <button id="export-test" class="secondary" style="margin-top:14px">🩺 Test source</button>
      <button id="export-stop" class="secondary hidden" style="margin-top:14px">&#9632; Stop</button>
      <button id="export-resume" class="secondary hidden" style="margin-top:14px">&#9654; Resume last</button>
      <button id="export-diagnostics" class="secondary hidden" style="margin-top:14px">Download stall diagnostics</button>
    </div>
  </section>

  <section class="pane" data-pane="browse">
    <h2>Browse a site's novels</h2>
    <p class="hint">Adds every novel a site has to your library with just its details
      (title, cover, description, chapter list), so you can browse and pick. To download
      the chapters too, use <b>Download a whole site</b> above, or select novels in the
      Reader and press <b>Get chapters</b>.</p>
    <label for="src-url">Site address</label>
    <input id="src-url" type="text" placeholder="https://novelsource.example/" />
    <div class="row">
      <button id="discover-btn">List novels</button>
      <button id="discover-stop" class="secondary hidden" style="margin-top:14px">&#9632; Stop</button>
    </div>
  </section>

  <section class="pane" data-pane="import">
    <h2>Import books</h2>
    <p class="hint">
      Carried this app and your downloads to a machine with no internet? Drop the
      export <code>.zip</code> here and every novel inside it goes back into your
      library — chapters, covers and all — ready to read offline. Single ebooks
      work too, and re-importing an archive updates what you already have instead
      of duplicating it.
    </p>
    <div id="drop-zone">
      <strong>Drop ZIPs or ebooks here</strong>
      <span class="muted">or click to choose files</span>
    </div>
    <input id="import-files" type="file" multiple
           accept=".zip,.epub,.cbz,.pdf,.docx,.mobi,.azw3,.fb2,.rtf,.txt"
           style="display:none" />
    <div id="import-status" class="muted"></div>
    <h3>A big ZIP already on this computer</h3>
    <p class="hint">Choose it here instead of dropping it: it is read where it is, with nothing
      uploaded or copied, however big it is. The library's drive needs about twice its size free.</p>
    <button id="import-local" class="secondary" style="margin-top:0">Choose a file on this computer…</button>
  </section>

  <section class="pane" data-pane="zip">
    <h2>Make a ZIP of your books</h2>
    <p class="hint">Packs the e-books you already have into one ZIP with a page to browse them.
      Nothing is downloaded from any site.</p>
    <label for="library-nsfw">NSFW novels</label>
    <select id="library-nsfw">
      <option value="include" selected>Include them</option>
      <option value="exclude">Leave them out</option>
      <option value="only">Export only NSFW</option>
    </select>
    <div class="row checkbox">
      <input id="library-completeonly" type="checkbox" />
      <label for="library-completeonly" style="margin:0">Only fully downloaded novels (every chapter downloaded)</label>
    </div>
    <label for="library-savedir">Save the ZIP to folder</label>
    <div class="row picker-row">
      <input id="library-savedir" type="text" placeholder="blank = download in the browser, e.g. D:\\Novels" />
      <button type="button" class="secondary browse" data-for="library-savedir">Browse…</button>
    </div>
    <div class="row">
      <button id="library-export">Make the ZIP</button>
      <button id="library-zip-stop" class="secondary hidden">Stop</button>
    </div>
    <p class="hint hidden" id="library-zip-status" role="status" style="margin-top:10px"></p>
    <p class="hint">Includes saved EPUBs, CBZ manga and books inside past export ZIPs, one copy
      per novel (the most complete one). NSFW is judged by a novel's tags (Adult, Mature, Smut,
      Ecchi, R-18&hellip;), so an untagged novel always counts as safe. A folder on another
      drive keeps the ZIP off a full system drive: it is written there and nowhere else.
      Extract the ZIP and open index.html to browse.</p>
    <h3>ZIPs from earlier site downloads</h3>
    <p class="hint">Click one to save it again.</p>
    <div id="library-list"><span class="muted">Sign in to see them.</span></div>
    <button id="library-refresh" class="secondary">Refresh the list</button>
  </section>

  <section class="pane" data-pane="library">
    <h2>Library and drives</h2>
    <p class="hint">Your library is everything the app keeps: novels, manga pages, e-books,
      ZIPs and its database. It all lives in one folder, which can be on any drive.</p>
    <div class="where"><p class="hint" id="move-status">Sign in to see where the library is.</p>
      <button id="move-cancel" class="secondary hidden">Cancel</button></div>

    <div id="found-box" class="hidden">
      <h3>Libraries found on your drives</h3>
      <p class="hint">Open one of these and the app runs from it after you close and reopen it.
        Nothing is copied, and the library in use now stays where it is.</p>
      <div id="found-list"></div>
    </div>

    <h3>Move the library to another drive</h3>
    <p class="hint">Everything is copied while you keep using the app (downloads pause), then it
      switches over by itself and removes the old copy. Nothing to close or reopen. Nothing is
      removed from the old place until the new one has it all.</p>
    <div id="drive-list"><span class="muted">Looking at your drives…</span></div>
    <details>
      <summary>Choose a folder yourself</summary>
      <label for="move-folder">Folder</label>
      <div class="row">
        <input id="move-folder" type="text" placeholder="e.g. D:\\LNCrawl" />
        <button type="button" class="secondary browse" data-for="move-folder" style="margin-top:0">Browse…</button>
        <button id="move-btn" class="secondary" style="margin-top:0">Move the library here</button>
        <button id="move-use" class="secondary" style="margin-top:0"
          title="For a library that is already in that folder. Nothing is copied.">Use the library in this folder</button>
      </div>
    </details>

    <h3>Books on a drive that is not always plugged in</h3>
    <div id="books-box"><p class="hint">Sign in to see where the books are.</p></div>
    <div id="books-split" class="hidden">
      <p class="hint">For a library too big for this computer: the database, settings and every
        novel's cover are copied to a folder here, and the books (chapters, pages, e-books) stay
        on the drive. The app then opens without the drive, shows the whole library, and reads
        what is on this computer; the rest says to plug the drive in. Nothing is removed from
        the drive.</p>
      <label for="books-here">Folder on this computer</label>
      <div class="row">
        <input id="books-here" type="text" />
        <button type="button" class="secondary browse" data-for="books-here" style="margin-top:0">Browse…</button>
        <button id="books-split-btn" class="secondary" style="margin-top:0">Keep the library here</button>
      </div>
    </div>
    <div id="books-make" class="hidden">
      <p class="hint">The library is on this computer. To make room, choose a folder on another
        drive for the books, then send novels there; the app keeps working when it is unplugged.</p>
      <label for="books-folder">Folder on the drive</label>
      <div class="row">
        <input id="books-folder" type="text" placeholder="e.g. F:\\Books" />
        <button type="button" class="secondary browse" data-for="books-folder" style="margin-top:0">Browse…</button>
        <button id="books-make-btn" class="secondary" style="margin-top:0">Keep books there</button>
      </div>
    </div>
    <div id="books-on" class="hidden">
      <label for="books-new">New novels go to</label>
      <select id="books-new">
        <option value="drive">the drive, when it is plugged in</option>
        <option value="here">this computer</option>
      </select>
      <div class="row">
        <button id="books-all-drive" class="secondary">Send every book to the drive</button>
        <button id="books-all-here" class="secondary" title="Only if this computer has room for all of them">Bring every book here</button>
      </div>
      <p class="hint">To keep just a few here, pick them in the Reader: Select, then "Keep on this computer".</p>
    </div>

    <h3>Space</h3>
    <p class="hint" id="storage-line">Sign in to see disk usage.</p>
    <div id="storage-largest" class="muted"></div>
    <div class="row">
      <button id="storage-refresh" class="secondary">Check what uses the space</button>
      <button id="storage-clean" class="secondary" title="Deletes the ZIPs made by site downloads (your novels stay). Backups are kept.">Delete old ZIPs</button>
    </div>

    <h3>Clean up extras</h3>
    <p class="hint">Duplicate novels are merged into their most complete copy (a copy with chapters
      the other lacks is kept). Older e-books of a novel go once a newer one is made. Leftovers are
      files nothing uses. Nothing on a drive that is not plugged in is touched.</p>
    <button id="extras-find" class="secondary" style="margin-top:0">Find extras</button>
    <div id="extras-box" class="hidden" style="margin-top:12px">
      <label class="check"><input type="checkbox" id="extras-duplicates" checked /> <span id="extras-duplicates-text">Duplicate novels</span></label>
      <div id="extras-duplicates-list" class="muted" style="max-height:180px;overflow:auto;margin:4px 0 10px 24px"></div>
      <label class="check"><input type="checkbox" id="extras-ebooks" checked /> <span id="extras-ebooks-text">Extra e-book copies</span></label>
      <label class="check"><input type="checkbox" id="extras-leftovers" checked /> <span id="extras-leftovers-text">Leftover files</span></label>
      <div id="extras-leftovers-list" class="muted" style="max-height:180px;overflow:auto;margin:4px 0 10px 24px"></div>
      <button id="extras-go">Clean up</button>
    </div>
    <div id="extras-progress" class="hidden" style="margin-top:10px">
      <progress id="extras-bar" max="1" value="0" style="width:100%"></progress>
      <p class="muted" id="extras-text"></p>
    </div>

    <h3>Backup</h3>
    <p class="hint">One ZIP of the database and every downloaded novel, saved to the library's
      exports folder and your Desktop.</p>
    <button id="backup-btn" class="secondary" style="margin-top:0">Create a backup</button>

    <h3>Read on your phone</h3>
    <p class="hint">Shows a code to scan. Your phone has to be on the same Wi-Fi as this computer.</p>
    <button id="phone-btn" class="secondary" style="margin-top:0">Show the code</button>
    <div id="phone-box" class="hidden" style="margin-top:14px">
      <div id="phone-qr" style="background:#fff;display:inline-block;padding:10px;border-radius:10px"></div>
      <p class="muted" id="phone-url" style="word-break:break-all"></p>
      <p class="hint">Scan with your phone's camera, then sign in with the same email and
        password. If it doesn't load, allow the app through Windows Firewall.</p>
    </div>
  </section>

  <section class="pane" data-pane="alerts">
    <h2>Alerts</h2>
    <p class="hint">Get pinged when a long overnight export finishes so you don't have to keep
      checking. A desktop notification pops on this machine; a webhook can reach your phone
      (Discord or Slack incoming webhook). Both are optional.</p>
    <div class="row checkbox">
      <input id="notify-desktop" type="checkbox" checked />
      <label for="notify-desktop" style="margin:0">Show a desktop notification when an export finishes</label>
    </div>
    <label for="notify-webhook">Webhook URL (optional)</label>
    <input id="notify-webhook" type="text" placeholder="https://discord.com/api/webhooks/..." />
    <button id="notify-save">Save alerts</button>
  </section>

  <section class="pane" data-pane="repair">
    <h2>Repair a novel</h2>
    <p class="hint">Check downloaded, missing, failed and empty chapters. Repair retries only
      chapter gaps and rebuilds the EPUB once all chapter text is available.</p>
    <label for="novel-id">Novel ID</label>
    <input id="novel-id" type="text" placeholder="e.g. 0c3b…" />
    <button id="completeness-btn">Check completeness</button>
    <button id="retry-btn" class="secondary">Retry missing and rebuild EPUB</button>
  </section>

  <div class="activity">
    <h2>What has happened</h2>
    <div id="log"><span class="muted">Nothing yet. What you start shows up here.</span></div>
  </div>
  <p class="build">__BUILD_INFO__</p>

  <dialog id="folder-picker" aria-labelledby="picker-title">
    <form method="dialog" class="picker">
      <h2 id="picker-title">Choose a folder</h2>
      <nav id="picker-trail" class="trail" aria-label="Where you are"></nav>
      <p id="picker-note" class="muted picker-note"></p>
      <div id="picker-list" class="picker-list" role="list"></div>
      <div id="picker-new" class="row hidden">
        <input id="picker-name" type="text" placeholder="Name of the new folder" />
        <button type="button" id="picker-make" class="secondary" style="margin-top:0">Make it</button>
      </div>
      <div class="row picker-actions">
        <button type="button" id="picker-new-btn" class="secondary">New folder</button>
        <span class="grow"></span>
        <button type="submit" value="cancel" class="secondary">Cancel</button>
        <button type="button" id="picker-use">Use this folder</button>
      </div>
    </form>
  </dialog>
</main>
</div>

<script>
(function () {
  var KEY = "lncrawl_token";

  // Accept a token passed in the URL (?authToken=...) — the same mechanism the
  // desktop app uses — then strip it from the address bar. This lets the page
  // auto-sign-in when opened from the local/desktop app.
  (function () {
    var params = new URLSearchParams(window.location.search);
    var t = params.get("authToken");
    if (t) {
      localStorage.setItem(KEY, t);
      params.delete("authToken");
      var qs = params.toString();
      window.history.replaceState({}, document.title,
        window.location.pathname + (qs ? "?" + qs : ""));
    }
  })();

  var logEl = document.getElementById("log");
  var firstLog = true;

  function token() { return localStorage.getItem(KEY) || ""; }

  // Point "Back to app" / "Open Reader" at their pages, carrying the session.
  (function () {
    var tok = token();
    var back = document.getElementById("back-link");
    if (back && tok) back.href = "/?authToken=" + encodeURIComponent(tok);
    var reader = document.getElementById("reader-link");
    if (reader && tok) reader.href = "/reader?authToken=" + encodeURIComponent(tok);
  })();

  var lastLine = null;
  var lastMsg = null;
  var lastCount = 1;

  function log(msg, cls) {
    if (firstLog) { logEl.innerHTML = ""; firstLog = false; }
    var ts = new Date().toLocaleTimeString();

    // The job pollers report on a timer, not on change, so a long download
    // says the same thing every few seconds. Appending each one buries the
    // history and — worse — makes slow-but-working look identical to stuck.
    // Repeats fold into the line already there, counting up with a clock that
    // keeps ticking: still moving, still the same place.
    if (lastLine && msg === lastMsg) {
      lastCount += 1;
      lastLine.textContent = "[" + ts + "] " + msg + " (×" + lastCount + ")";
      logEl.scrollTop = logEl.scrollHeight;
      return;
    }

    var line = document.createElement("div");
    if (cls) line.className = cls;
    line.textContent = "[" + ts + "] " + msg;
    logEl.appendChild(line);
    lastLine = line;
    lastMsg = msg;
    lastCount = 1;
    logEl.scrollTop = logEl.scrollHeight;
  }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers["Content-Type"] = "application/json";
    if (token()) opts.headers["Authorization"] = "Bearer " + token();
    return fetch("/api" + path, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
        if (!res.ok) {
          // ServerError responses look like { error: <reason>, detail: <extra> }
          var msg = data && (data.error || data.message);
          var extra = data && data.detail;
          var detail = msg
            ? (extra && extra !== msg ? msg + " (" + extra + ")" : msg)
            : (extra || text || res.statusText);
          throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        }
        return data;
      });
    });
  }

  // ---- the menu: one task on screen at a time ----
  var PANES = ["download", "browse", "import", "zip", "library", "repair", "alerts"];
  function showPane(name) {
    if (PANES.indexOf(name) < 0) name = "download";
    document.querySelectorAll(".pane[data-pane]").forEach(function (pane) {
      pane.classList.toggle("shown", pane.getAttribute("data-pane") === name);
    });
    document.querySelectorAll(".menu button.go").forEach(function (button) {
      if (button.getAttribute("data-go") === name) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    try { history.replaceState(null, "", "#" + name); } catch (e) {}
    if (name === "library" && token()) { loadMove(); loadPlaces(); loadStorage(); loadBooks(); }
  }
  document.querySelectorAll(".menu button.go").forEach(function (button) {
    button.addEventListener("click", function () {
      showPane(button.getAttribute("data-go"));
      window.scrollTo(0, 0);
    });
  });

  function refreshAuthUI() {
    document.body.classList.toggle("signed-out", !token());
    var inEl = document.getElementById("logged-in");
    var outEl = document.getElementById("logged-out");
    if (token()) {
      inEl.classList.remove("hidden");
      outEl.classList.add("hidden");
      api("/auth/me", { method: "GET" })
        .then(function (u) {
          document.getElementById("who").textContent = (u && (u.email || u.name)) || "user";
          window._lncrawlUserId = u && u.id;
          window._lncrawlIsAdmin = !!(u && u.role === "admin");
          loadActiveExports(u && u.id);
          loadActiveDiscoveries(u && u.id);
          loadLibrary(u && u.id);
          loadNotifyConfig();
          loadMove();
          loadPlaces();
          loadZip();
        })
        .catch(function () {
          // stale/invalid token
          localStorage.removeItem(KEY);
          refreshAuthUI();
        });
    } else {
      inEl.classList.add("hidden");
      outEl.classList.remove("hidden");
    }
  }

  function requireAuth() {
    if (token()) return true;
    log("Please sign in first.", "log-err");
    return false;
  }

  function busy(btn, on) { btn.disabled = on; }

  document.getElementById("login-btn").addEventListener("click", function () {
    var btn = this;
    var email = document.getElementById("email").value.trim();
    var password = document.getElementById("password").value;
    if (!email || !password) { log("Enter email and password.", "log-err"); return; }
    busy(btn, true);
    api("/auth/login", { method: "POST", body: JSON.stringify({ email: email, password: password }) })
      .then(function (res) {
        localStorage.setItem(KEY, res.token);
        document.getElementById("password").value = "";
        log("Signed in.", "log-ok");
        refreshAuthUI();
      })
      .catch(function (e) { log("Sign in failed: " + e.message, "log-err"); })
      .finally(function () { busy(btn, false); });
  });

  document.getElementById("logout-btn").addEventListener("click", function () {
    localStorage.removeItem(KEY);
    log("Signed out.", "log-info");
    refreshAuthUI();
  });

  // The API serializes JobStatus as an integer; map it to a name. Strings pass
  // through in case that ever changes.
  var STATUS = ["PENDING", "RUNNING", "SUCCESS", "FAILED", "CANCELED", "PAUSED"];
  function statusName(s) {
    return typeof s === "number" ? (STATUS[s] || String(s)) : s;
  }
  function isActive(s) { return s === "PENDING" || s === "RUNNING"; }

  // Job polling backs off while the tab is hidden: a background tab polls at
  // most every 30 s instead of every 2-4 s, and catches up as soon as it is shown.
  var waiting = [];
  function later(fn, ms) {
    var entry = { fn: fn, t: 0 };
    entry.t = setTimeout(function () {
      var i = waiting.indexOf(entry); if (i >= 0) waiting.splice(i, 1);
      fn();
    }, document.hidden ? Math.max(ms, 30000) : ms);
    waiting.push(entry);
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) return;
    var due = waiting; waiting = [];
    due.forEach(function (e) { clearTimeout(e.t); e.fn(); });
  });

  // Jobs share a few workers. Say what a waiting job is waiting behind, once per change.
  function explainWaiting(id, label) {
    explainWaiting._said = explainWaiting._said || {};
    api("/jobs?status=1&limit=20", { method: "GET" })
      .then(function (res) {
        var running = ((res && res.items) || []).filter(function (j) { return j.id !== id; });
        var names = running.slice(0, 3).map(function (j) { return j.job_title || "job"; });
        var key = names.join("|") || "none";
        if (explainWaiting._said[id] === key) return;
        explainWaiting._said[id] = key;
        log(label + " · waiting for a free worker" +
          (names.length ? " — running: " + names.join("; ") + (running.length > 3 ? " and more" : "") : "") + ".",
          "log-info");
      })
      .catch(function () {
        log(label + " · PENDING", "log-info");
      });
  }

  function pollJob(id, label, onEnd, fails, full) {
    // asked without its long lists while it runs; once more with them when it ends
    api("/job/" + id + (full ? "" : "?light=true"), { method: "GET" })
      .then(function (job) {
        fails = 0;
        var status = statusName(job.status);
        if (!full && !isActive(status)) { pollJob(id, label, onEnd, 0, true); return; }
        var ex = job.extra || {};
        if (status === "PENDING") {
          explainWaiting(id, label);
        } else if (status === "RUNNING" && ex.phase === "discovering") {
          log(label + " · searching the source… " + (ex.found || 0) + " novels found (" +
            (ex.searched || 0) + "/" + (ex.seeds || 0) + " searches + sitemap pages)", "log-info");
        } else if (status === "RUNNING" && ex.phase === "fetching") {
          log(label + " · fetching novel details " + job.done + "/" + job.total +
            " (" + (ex.total_found || 0) + " found" + (ex.truncated ? ", capped" : "") + ")", "log-info");
        } else {
          log(label + " · " + status + " (" + job.done + "/" + job.total + ")", "log-info");
        }
        if (status === "SUCCESS" && ex.phase === "fetching" && !ex.full && label === "Discover") {
          log("  Added the details of " + (ex.discovered ? ex.discovered.length : job.total) + " novel(s). No chapters were downloaded — tick " +
            "“Also download every chapter” to fetch them too.", "log-info");
        }
        if (ex.tier_capped && !isActive(status)) {
          log("  Found " + ex.tier_found + " novels; your account adds up to " + ex.tier_capped +
            " per discovery, so the first " + ex.tier_capped + " were added.", "log-info");
        }
        if (status === "SUCCESS" && ex.discovery_capped) {
          log("  ⚠ Discovery stopped at its 10 minute cap, so this is only part of the catalogue.", "log-info");
        }
        if (job.extra && job.extra.completeness) {
          var c = job.extra.completeness;
          log((c.complete ? "Complete" : "Incomplete") + " — " + c.downloaded + "/" + c.total +
            " downloaded, " + c.missing + " missing, " + c.failed + " failed, " + c.empty + " empty.", "log-info");
        }
        if (isActive(status)) {
          later(function () { pollJob(id, label, onEnd, fails); }, 3000);
          return;
        }
        if (status === "FAILED" && job.error) {
          log(label + " failed: " + job.error, "log-err");
        }
        if (label === "Discover") {
          notifyEnd("Discovery " + (status === "SUCCESS" ? "finished" : status.toLowerCase()),
            (ex.discovered ? ex.discovered.length : job.total) + " novel(s)" +
            (status === "FAILED" && job.error ? " — " + String(job.error).split("\\n").pop() : ""));
        }
        if (onEnd) onEnd(status);
      })
      .catch(function (e) {
        if (/No such job/.test(e.message)) {
          log(label + " · the job is no longer there.", "log-err");
          if (onEnd) onEnd("GONE");
          return;
        }
        // A dropped connection or a restart is not the end of the job: ask
        // again, less often each time, instead of leaving Stop and the
        // progress stuck where they were.
        fails = (fails || 0) + 1;
        if (fails === 1) log("Status check failed: " + e.message + " — trying again.", "log-err");
        later(function () { pollJob(id, label, onEnd, fails); }, Math.min(30000, 3000 * Math.pow(2, fails - 1)));
      });
  }

  // Discovery jobs that are still going, so Stop can end them (and the novel
  // fetches they queued) — including ones started before this page was opened.
  var activeDiscoveries = {};

  function updateDiscoverStop() {
    var stop = document.getElementById("discover-stop");
    stop.classList.toggle("hidden", !Object.keys(activeDiscoveries).length);
  }

  function watchDiscovery(id) {
    activeDiscoveries[id] = 1;
    updateDiscoverStop();
    pollJob(id, "Discover", function () {
      delete activeDiscoveries[id];
      updateDiscoverStop();
    });
  }

  function loadActiveDiscoveries(userId) {
    var q = "/jobs?type=52&limit=10" + (userId ? "&user_id=" + encodeURIComponent(userId) : "");
    api(q, { method: "GET" })
      .then(function (res) {
        ((res && res.items) || []).forEach(function (j) {
          if (!isActive(statusName(j.status)) || activeDiscoveries[j.id]) return;
          log("Resuming discovery " + String(j.id).slice(0, 8) + "…", "log-info");
          watchDiscovery(j.id);
        });
      })
      .catch(function () {});
  }

  document.getElementById("discover-stop").addEventListener("click", function () {
    var ids = Object.keys(activeDiscoveries);
    if (!ids.length) return;
    log("Stopping discovery… (novels already fetched are kept)", "log-info");
    ids.forEach(function (id) {
      api("/job/" + id + "/cancel", { method: "POST" })
        .then(function () { log("Discovery stopped.", "log-ok"); })
        .catch(function (e) { log("Stop failed: " + e.message, "log-err"); });
    });
  });

  document.getElementById("discover-btn").addEventListener("click", function () {
    if (!requireAuth()) return;
    var btn = this;
    var url = document.getElementById("src-url").value.trim();
    var full = false;  // chapters go through "Download a whole site" or the Reader
    if (!url) { log("Enter a source URL.", "log-err"); return; }
    askToNotify();
    busy(btn, true);
    log("Queuing discovery for " + url + " …", "log-info");
    api("/job/create/discover-source", { method: "POST", body: JSON.stringify({ url: url, full: full }) })
      .then(function (job) {
        log("Discovery job created: " + job.id, "log-ok");
        watchDiscovery(job.id);
      })
      .catch(function (e) { log("Discovery failed: " + e.message, "log-err"); })
      .finally(function () { busy(btn, false); });
  });

  function downloadBlob(jobId, name) {
    log("Building download …", "log-info");
    fetch("/api/job/" + jobId + "/export", { headers: { "Authorization": "Bearer " + token() } })
      .then(function (res) { if (!res.ok) throw new Error("HTTP " + res.status); return res.blob(); })
      .then(function (blob) {
        var a = document.createElement("a");
        var objUrl = URL.createObjectURL(blob);
        a.href = objUrl; a.download = name || "novels.zip";
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function () { URL.revokeObjectURL(objUrl); }, 10000);
        log("Saved " + (name || "novels.zip") + " — check your downloads.", "log-ok");
      })
      .catch(function (e) { log("Download failed: " + e.message, "log-err"); });
  }

  var activeExportId = null;
  var lastExportId = null;

  function setExportControls(activeId, resumableId) {
    activeExportId = activeId;
    if (resumableId) lastExportId = resumableId;
    var stop = document.getElementById("export-stop");
    var res = document.getElementById("export-resume");
    if (activeId) { stop.classList.remove("hidden"); } else { stop.classList.add("hidden"); }
    if (!activeId && lastExportId) { res.classList.remove("hidden"); } else { res.classList.add("hidden"); }
  }

  function rateNote(ex) {
    var bits = [];
    // the rate the tuner has reached (older jobs kept it in requests_per_sec)
    var rps = ex.current_rps || ex.requests_per_sec;
    if (rps) bits.push(rps + " req/s");
    if (ex.adaptive_delay) bits.push("+" + ex.adaptive_delay + "s/novel");
    return bits.length ? " [" + bits.join(", ") + "]" : "";
  }

  // why a lane is pausing before its next novel: polite mode's own short pause,
  // or a longer one because the site has been failing (older jobs never said)
  function pauseNote(ex) {
    var why = ex.pause_reason === "polite" ? "polite mode" : "after source errors";
    return "pausing " + (ex.adaptive_delay || 0) + "s before the next novel (" + why + ")";
  }

  // " at 14:05" for a wait that ends at resume_at
  function untilNote(ex) {
    if (!ex.resume_at) return "";
    var d = new Date(ex.resume_at);
    return " at " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) +
      (d.toDateString() !== new Date().toDateString() ? " tomorrow" : "");
  }

  function timeLeft(seconds) {
    if (seconds == null || seconds < 0) return "";
    var m = Math.round(seconds / 60);
    if (m < 1) return "under a minute";
    if (m < 60) return m + " min";
    var h = Math.floor(m / 60), d = Math.floor(h / 24);
    if (d >= 1) return d + "d " + (h % 24) + "h";
    return h + "h " + (m % 60) + "m";
  }

  // How far a site download is: by chapters, once they are known. Novels alone
  // ran ahead through those already downloaded and then sat on the long ones.
  function progressNote(job, ex) {
    if (!ex.chapters_total) return job.done + "/" + job.total;
    var pct = Math.floor(100 * ex.chapters_done / ex.chapters_total);
    var out = pct + "% of chapters (" + count(ex.chapters_done) + " of " + count(ex.chapters_total) +
      (ex.novels_unlisted ? ", and " + count(ex.novels_unlisted) + " novel(s) not looked at yet" : "") + ")";
    return out + " · " + job.done + " of " + job.total + " novels done";
  }

  // "~2h 10m left at this pace", plus the other novels downloading alongside
  function paceNote(ex) {
    var out = "";
    var left = timeLeft(ex.eta_seconds);
    if (left) out += " · ~" + left + " left at this pace";
    if (ex.chapters_per_min) out += " (" + ex.chapters_per_min + " ch/min)";
    var cuts = ex.slowdowns || {};
    var why = [];
    if (cuts.refused) why.push(cuts.refused + "× because the site refused requests");
    if (cuts.slow) why.push(cuts.slow + "× because many requests timed out");
    if (why.length) out += " · slowed " + why.join(", ") + " in the last 10 min";
    return out;
  }

  // The novels downloading now, always in the same order, so the line reads
  // the same from one update to the next instead of swapping places.
  function nowNote(ex) {
    var list = (ex.active_novels || []).slice();
    if (!list.length && ex.current_title) {
      list = [{ title: ex.current_title, done: ex.current_chapters || 0, total: ex.current_total_chapters || 0 }];
    }
    list.sort(function (a, b) { return String(a.title).localeCompare(String(b.title)); });
    return list.map(function (a) {
      return a.title + (a.total ? " — ch " + a.done + "/" + a.total : "");
    }).join(" · ");
  }

  // A browser notification when a long job ends, if the user allowed them.
  function askToNotify() {
    try {
      if (window.Notification && Notification.permission === "default") Notification.requestPermission();
    } catch (e) {}
  }
  function notifyEnd(title, body) {
    try {
      if (window.Notification && Notification.permission === "granted") new Notification(title, { body: body });
    } catch (e) {}
  }

  // One download runs per site (and a few sites at once), so a queued one is
  // waiting on another — the same site's, if there is one. Name it, and point
  // Stop at it, so a queue that never moves can be cleared from here.
  function explainQueued(id, domain) {
    explainQueued._said = explainQueued._said || {};
    api("/jobs?type=53&status=1&limit=20", { method: "GET" })
      .then(function (res) {
        var others = ((res && res.items) || []).filter(function (j) { return j.id !== id; });
        var same = others.filter(function (j) { return (j.extra || {}).domain === domain; });
        var blocker = same[0] || others[0];
        var key = blocker ? blocker.id : "none";
        if (explainQueued._said[id] === key) return;
        explainQueued._said[id] = key;
        if (blocker) {
          var bx = blocker.extra || {};
          var what = (bx.domain || "another export") + (bx.current_title ? " (" + bx.current_title + ")" : "");
          setExportControls(blocker.id, null);
          log("⏳ Waiting for a free slot (another site download, " + what + ", is using it). " +
            "One download runs per site, and different sites run side by side.", "log-info");
        } else {
          log("⏳ Queued — waiting for the previous export to finish shutting down. " +
            "If this doesn't start within a few minutes, restart the app; queued exports resume.", "log-info");
        }
      })
      .catch(function () {});
  }

  function pollExport(id, fails) {
    fails = fails || 0;
    api("/job/" + id + "?light=true", { method: "GET" })
      .then(function (job) {
        var status = statusName(job.status);
        var ex = job.extra || {};
        if (status === "RUNNING") setExportControls(id, id);
        if (status === "PENDING" && ex.phase === "starting") {
          // taken off the queue; a download process is loading the app for it
          log("Starting — loading a download process for it (the first one can take a minute)…", "log-info");
        } else if (status === "PENDING") {
          explainQueued(id, ex.domain);
        } else if (status === "RUNNING" && ex.phase === "discovering") {
          log("Discovering novels… " + (ex.found || 0) + " found (" + job.done + "/" + job.total + " searches + sitemap pages)", "log-info");
        } else if (status === "RUNNING" && ex.phase === "solving-challenge") {
          log("Site is blocking requests — solving the Cloudflare challenge in a browser…", "log-info");
        } else if (status === "RUNNING" && (ex.phase === "downloading" || ex.phase === "waiting-between-novels") && ex.current_title) {
          // a pause between novels is one lane's: the others carry on, so show them all
          var resumed = ex.resumed ? " · " + ex.resumed + " reused from last run" : "";
          log("Downloading " + progressNote(job, ex) + ": " + nowNote(ex) + rateNote(ex) + resumed + paceNote(ex) +
            (ex.phase === "waiting-between-novels" ? " · " + pauseNote(ex) : ""), "log-info");
          if (ex.last_fail_title && ex.last_fail_reason) {
            pollExport._lastFailure = pollExport._lastFailure || {};
            var failureKey = ex.last_fail_title + ":" + ex.last_fail_reason + ":" + (ex.last_fail_detail || "");
            if (pollExport._lastFailure[id] !== failureKey) {
              pollExport._lastFailure[id] = failureKey;
              log("  ⚠ last issue: " + ex.last_fail_title + " — " + ex.last_fail_reason +
                (ex.last_fail_detail ? " (" + ex.last_fail_detail + ")" : ""), "log-err");
            }
          }
        } else if (status === "RUNNING" && ex.phase === "downloading-images" && ex.current_title) {
          // a manga's pages come after its chapters and can take far longer
          log("Downloading pages " + job.done + "/" + job.total + ": " + ex.current_title +
            " — image " + (ex.current_images || 0) + "/" + (ex.current_total_images || 0) +
            rateNote(ex) + paceNote(ex), "log-info");
        } else if (status === "RUNNING" && ex.phase === "waiting-between-novels") {
          log("Download " + job.done + "/" + job.total + ": " + pauseNote(ex) + "; downloaded chapters are kept.", "log-info");
        } else if (status === "RUNNING" && ex.phase === "retry-waiting") {
          log("Round " + ((ex.retry || 0) + 1) + " for what's left — " +
            (ex.quiet_rounds ? "the site gave nothing new last round, so it's resting it" : "waiting briefly") +
            untilNote(ex) + ". Nothing is lost while it waits.", "log-info");
        } else if (status === "RUNNING" && ex.phase === "cooling-down") {
          var why = { "stalled": "downloads stopped responding, so it started fresh downloaders",
            "blocked-discovery": "the site isn't answering searches (likely blocking for now)",
            "restarting": "something unexpected went wrong, so it restarts from where it was" }[ex.cool_reason] || "the site needs a rest";
          log("Pausing — " + why + "; trying again" + untilNote(ex) + ".", "log-info");
        } else if (status === "RUNNING" && ex.phase === "disk-full") {
          var room = ex.disk_free != null ? " Only " + Math.floor(ex.disk_free / 1048576) + " MB is free" +
            (ex.disk_path ? " for " + ex.disk_path : "") + "." : "";
          if (ex.disk_need) {
            log("⚠ Paused before the drive fills up: " + (ex.disk_title || "the next novel") + " needs about " +
              fmtSize(ex.disk_need) + " and only " + (fmtSize(ex.disk_free) || "0 MB") + " is free" +
              (ex.disk_path ? " for " + ex.disk_path : "") + ". Nothing of it was downloaded. Move the " +
              "library to a bigger drive (Library and drives) or free up space — it carries on by itself" +
              untilNote(ex) + ".", "log-err");
          } else
          log("⚠ The disk is full: downloads wait so nothing is fetched and thrown away." + room +
            " Free up some space, or move the data folder to a bigger drive — it carries on by itself" +
            untilNote(ex) + ".", "log-err");
        } else if (status === "RUNNING" && ex.phase === "checking-updates") {
          log("Checking finished novels for new chapters… (" + job.done + "/" + job.total + ")", "log-info");
        } else {
          log("Export · " + status + " (" + job.done + "/" + job.total + ")", "log-info");
        }
        if (isActive(status)) {
          later(function () { pollExport(id, 0); }, 4000);
        } else if (status === "SUCCESS") {
          setExportControls(null, id);
          var extra = job.extra || {};
          var parts = [(extra.complete != null ? extra.complete : extra.exported || 0) + " complete"];
          if (extra.incomplete) parts.push(extra.incomplete + " partial");
          if (extra.skipped) parts.push(extra.skipped + " skipped (already had)");
          if (extra.left_out) parts.push(extra.left_out + " partly downloaded left out of the ZIP" +
            (extra.zip_only ? "" : " (in your library)"));
          if (extra.failed) parts.push(extra.failed + " failed");
          log((extra.library_only ? "Done — in your library: " : "Export done: ") + parts.join(", ") +
            " of " + (extra.total_novels || 0) + "." + (extra.library_only ? " Open the Reader to read them." : "") +
            (extra.zip_only ? " Nothing it downloaded was kept in your library." : ""),
            extra.failed ? "log-info" : "log-ok");
          notifyEnd("Export finished", (extra.domain || "Export") + ": " + parts.join(", ") + " of " + (extra.total_novels || 0) + ".");
          if (extra.discovery_capped) {
            log("  ⚠ Discovery stopped at its time cap, so this is only part of the source's " +
              "catalogue. Raise “Max discovery (min)” in Advanced options and run it again — " +
              "finished novels are reused.", "log-info");
          }
          (extra.fail_reasons || []).forEach(function (fr) {
            log("  " + fr.count + "× " + fr.reason, "log-err");
          });
          if (extra.saved_to) log((extra.save_dir ? "Saved to: " : "Saved to Desktop: ") + extra.saved_to, "log-ok");
          // a ZIP already in the folder that was picked for it needs no second copy
          if (extra.export_file && !extra.save_dir) downloadBlob(id, extra.export_name);
          loadLibrary(window._lncrawlUserId);
        } else {
          setExportControls(null, id);
          if (ex.stall_diagnostics) {
            var diagnosticButton = document.getElementById("export-diagnostics");
            diagnosticButton.dataset.jobId = id;
            diagnosticButton.classList.remove("hidden");
          }
          log("Export ended: " + status + (job.error ? " — " + job.error : "") +
            ". Use the Resume last button to pick up where it left off.", "log-err");
          notifyEnd(status === "FAILED" ? "Export failed" : "Export stopped",
            (ex.domain || "Export") + " — " + (status === "FAILED" ? String(job.error || "").split("\\n").pop() :
              "stopped after " + job.done + " of " + job.total + " novels") + ". Resume picks up where it left off.");
        }
      })
      .catch(function (e) {
        // A failed poll cannot establish whether the server or export is still
        // running. Keep reconnecting without claiming the job is healthy.
        var n = fails + 1;
        if (n === 1) log("Status check hiccup — still retrying… (" + e.message + ")", "log-err");
        else if (n === 15) log("Still can't reach the server — export status is unknown; retrying every 15s. Check that the app is still running before starting another export.", "log-err");
        later(function () { pollExport(id, n); }, n < 15 ? 8000 : 15000);
      });
  }

  document.getElementById("export-stop").addEventListener("click", function () {
    if (!activeExportId) return;
    var id = activeExportId;
    log("Stopping export… (finished novels are kept; use Resume to continue)", "log-info");
    api("/job/" + id + "/cancel", { method: "POST" })
      .then(function () { setExportControls(null, id); log("Export stopped.", "log-ok"); })
      .catch(function (e) { log("Stop failed: " + e.message, "log-err"); });
  });

  document.getElementById("export-diagnostics").addEventListener("click", function () {
    var id = this.dataset.jobId;
    if (!id || !requireAuth()) return;
    fetch("/api/job/" + encodeURIComponent(id) + "/stall-diagnostics", {
      headers: { "Authorization": "Bearer " + token() }
    }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.blob();
    }).then(function (blob) {
      var url = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = url; link.download = "export-stall-" + id + ".log";
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
    }).catch(function (error) { log("Couldn't download diagnostics: " + error.message, "log-err"); });
  });

  document.getElementById("export-resume").addEventListener("click", function () {
    if (!lastExportId) { log("No previous export to resume.", "log-err"); return; }
    log("Resuming the last export (skips novels already finished)…", "log-info");
    api("/job/" + lastExportId + "/replay", { method: "POST" })
      .then(function (job) {
        log("Resumed as job " + job.id, "log-ok");
        setExportControls(job.id, job.id);
        pollExport(job.id);
      })
      .catch(function (e) { log("Resume failed: " + e.message, "log-err"); });
  });

  // Reattach to running exports on page load (type 53 = EXPORT_SOURCE) so closing
  // and reopening Tools still shows live progress.
  function loadActiveExports(userId) {
    var q = "/jobs?type=53&limit=10" + (userId ? "&user_id=" + encodeURIComponent(userId) : "");
    api(q, { method: "GET" })
      .then(function (res) {
        var items = (res && res.items) || [];
        items.forEach(function (j) {
          var s = statusName(j.status);
          if (s !== "RUNNING" && s !== "PENDING") return;
          var dom = (j.extra && j.extra.domain) || "export";
          log("Resuming " + dom + " (" + String(j.id).slice(0, 8) + ")…", "log-info");
          pollExport(j.id);
        });
      })
      .catch(function () {});
  }

  function buildExportBody() {
    var url = document.getElementById("exp-url").value.trim();
    if (!url) { log("Enter a source URL.", "log-err"); return null; }
    var body = { url: url, format: document.getElementById("exp-format").value };
    body.library_only = document.getElementById("exp-make").value === "library";
    body.zip_only = document.getElementById("exp-make").value === "ziponly";
    body.until_done = document.getElementById("exp-untildone").checked;
    var limitRaw = document.getElementById("exp-limit").value.trim();
    if (limitRaw) {
      var limit = parseInt(limitRaw, 10);
      if (!isNaN(limit) && limit > 0) body.limit = limit;
    }
    var retriesRaw = document.getElementById("exp-retries").value.trim();
    if (retriesRaw) {
      var retries = parseInt(retriesRaw, 10);
      if (!isNaN(retries) && retries >= 0) body.retries = retries;
    }
    var discRaw = document.getElementById("exp-discovery").value.trim();
    if (discRaw) {
      var disc = parseInt(discRaw, 10);
      if (!isNaN(disc) && disc > 0) body.discovery_minutes = disc;
    }
    var maxchRaw = document.getElementById("exp-maxch").value.trim();
    if (maxchRaw) {
      var maxch = parseInt(maxchRaw, 10);
      if (!isNaN(maxch) && maxch > 0) body.max_chapters = maxch;
    }
    var rpsRaw = document.getElementById("exp-rps").value.trim();
    if (rpsRaw) {
      var rps = parseFloat(rpsRaw);
      if (!isNaN(rps) && rps > 0) body.requests_per_sec = rps;
    }
    body.polite = document.getElementById("exp-polite").checked;
    body.dedupe = document.getElementById("exp-dedupe").checked;
    body.resume = document.getElementById("exp-resume").checked;
    body.auto_tune = document.getElementById("exp-autotune").checked;
    body.update_only = document.getElementById("exp-update").checked;
    body.use_sitemap = document.getElementById("exp-sitemap").checked;
    body.parallel_novels = parseInt(document.getElementById("exp-lanes").value, 10) || 2;
    // a roomier drive: the ZIP is written there only, not also to the Desktop
    var saveDir = document.getElementById("exp-savedir").value.trim();
    if (saveDir && !body.library_only) body.save_dir = saveDir;
    if (!body.library_only && document.getElementById("exp-completeonly").checked) body.complete_only = true;
    return body;
  }

  // the e-book format only matters when e-books are being made
  function syncMake() {
    var make = document.getElementById("exp-make").value;
    document.getElementById("exp-zip-opts").classList.toggle("hidden", make === "library");
    document.getElementById("exp-ziponly-hint").classList.toggle("hidden", make !== "ziponly");
    document.getElementById("exp-completeonly-label").textContent = make === "ziponly"
      ? "Only fully downloaded novels in the ZIP (partly downloaded ones are dropped)"
      : "Only fully downloaded novels in the ZIP (partly downloaded ones stay in the library)";
  }
  document.getElementById("exp-make").addEventListener("change", syncMake);
  syncMake();

  document.getElementById("export-btn").addEventListener("click", function () {
    if (!requireAuth()) return;
    var btn = this;
    var body = buildExportBody();
    if (!body) return;
    askToNotify();
    busy(btn, true);
    log("Starting to download every novel from " + body.url +
      (body.until_done ? " — it keeps going until everything is in, so you can leave it." : " …"), "log-info");
    api("/job/create/export-source", { method: "POST", body: JSON.stringify(body) })
      .then(function (job) {
        log("Download started (job " + String(job.id).slice(0, 8) + "). Closing this page doesn't stop it — " +
          "follow, pause or resume it on the Downloads page.", "log-ok");
        pollExport(job.id);
      })
      .catch(function (e) { log("Export failed: " + e.message, "log-err"); })
      .finally(function () { busy(btn, false); });
  });

  // Speed presets — one click sets the pacing fields instead of guessing numbers.
  var PRESETS = {
    fast:     { rps: "",     polite: false, retries: "3", note: "Fast — no throttle (friendly sites)" },
    balanced: { rps: "1",    polite: true,  retries: "3", note: "Balanced — good default" },
    gentle:   { rps: "0.25", polite: true,  retries: "5", note: "Gentle — for sites that block hard" }
  };
  function applyPreset(name) {
    var p = PRESETS[name]; if (!p) return;
    document.getElementById("exp-rps").value = p.rps;
    document.getElementById("exp-polite").checked = p.polite;
    document.getElementById("exp-retries").value = p.retries;
    document.getElementById("preset-note").textContent = p.note;
    var btns = document.querySelectorAll("#exp-presets .preset");
    for (var i = 0; i < btns.length; i++) {
      btns[i].classList.toggle("active", btns[i].getAttribute("data-preset") === name);
    }
  }
  (function () {
    var btns = document.querySelectorAll("#exp-presets .preset");
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener("click", function () { applyPreset(this.getAttribute("data-preset")); });
    }
    applyPreset("balanced");
  })();

  // Test source — a tiny capped export (1 novel, 3 chapters) to confirm a source
  // actually works with the current settings before committing to a big run.
  document.getElementById("export-test").addEventListener("click", function () {
    if (!requireAuth()) return;
    var btn = this;
    var body = buildExportBody();
    if (!body) return;
    body.limit = 1; body.max_chapters = 3; body.dedupe = false; body.resume = false;
    body.retries = 0; body.discovery_minutes = 2;
    busy(btn, true);
    log("Testing " + body.url + " — fetching 1 novel / 3 chapters (saves a small test zip)…", "log-info");
    api("/job/create/export-source", { method: "POST", body: JSON.stringify(body) })
      .then(function (job) { log("Test started: " + job.id, "log-ok"); pollExport(job.id); })
      .catch(function (e) { log("Test failed to start: " + e.message, "log-err"); })
      .finally(function () { busy(btn, false); });
  });

  function fmtSize(bytes) {
    if (!bytes) return "";
    var mb = bytes / (1024 * 1024);
    return mb >= 1024 ? (mb / 1024).toFixed(1) + " GB" : mb.toFixed(1) + " MB";
  }
  // ---- Import novels from an archive ----
  var dropZone = document.getElementById("drop-zone");
  var importInput = document.getElementById("import-files");
  var importStatus = document.getElementById("import-status");

  function dragHasFiles(e) {
    var types = (e.dataTransfer && e.dataTransfer.types) || [];
    for (var i = 0; i < types.length; i++) {
      if (types[i] === "Files") return true;
    }
    return false;
  }

  function setDragging(on) {
    if (dropZone) dropZone.classList.toggle("dragging", on);
  }

  // Dropping a file anywhere else would make the browser navigate to it and
  // throw the page away mid-upload, so the whole window swallows the event.
  ["dragenter", "dragover"].forEach(function (name) {
    window.addEventListener(name, function (e) {
      if (!dragHasFiles(e)) return;
      e.preventDefault();
      setDragging(true);
    });
  });
  window.addEventListener("dragleave", function (e) {
    if (!e.relatedTarget) setDragging(false);
  });
  window.addEventListener("drop", function (e) {
    e.preventDefault();
    setDragging(false);
  });

  dropZone.addEventListener("drop", function (e) {
    e.preventDefault();
    setDragging(false);
    uploadArchives(e.dataTransfer && e.dataTransfer.files);
  });
  dropZone.addEventListener("click", function () { importInput.click(); });
  importInput.addEventListener("change", function () {
    uploadArchives(importInput.files);
    importInput.value = "";
  });

  function pollImport(id, name, fails) {
    api("/job/" + id + "?light=true", { method: "GET" })
      .then(function (job) {
        var ex = job.extra || {};
        var status = statusName(job.status);  // a number from the API, a name here
        if (isActive(status)) {
          var seen = (ex.imported || 0) + (ex.updated || 0);
          importStatus.textContent = name + ": " +
            (ex.books ? "book " + Math.min(ex.books, (ex.books_read || 0) + 1) + " of " + ex.books +
              " (" + Math.floor(100 * (ex.books_read || 0) / ex.books) + "%)" + (ex.current ? ", " + ex.current : "")
              : (ex.current ? "reading " + ex.current : "starting")) +
            " — " + seen + " novel(s), " + (ex.chapters || 0) + " chapters so far";
          later(function () { pollImport(id, name, 0); }, 2000);
          return;
        }
        importStatus.textContent = "";
        if (status === "SUCCESS") {
          log("Imported " + name + ": " + (ex.imported || 0) + " new, " +
            (ex.updated || 0) + " updated, " + (ex.chapters || 0) + " chapters.", "log-ok");
          if (ex.failed) {
            log("  " + ex.failed + " file(s) in the archive could not be read.", "log-info");
          }
          log("  Open the Reader to read them offline.", "log-info");
        } else {
          log("Import failed for " + name + (job.error ? " — " + job.error : "") + ".", "log-err");
        }
        loadLibrary(window._lncrawlUserId);
      })
      .catch(function (e) {
        // the import keeps running server-side, so a hiccup is not the end
        var n = (fails || 0) + 1;
        if (n === 1) log("Status check hiccup — still watching the import…", "log-err");
        later(function () { pollImport(id, name, n); }, n < 10 ? 4000 : 10000);
      });
  }

  // Each file goes up in a request of its own, one after another. Sending them
  // all in one request meant a single dropped connection or oversized file lost
  // every file after it, so a big drop only ever got a few novels in at a time.
  // Bigger than this, an upload is copied twice on its way in and can fill the
  // drive: an admin is shown how to import it where it is instead.
  var BIG_UPLOAD = 2 * 1024 * 1024 * 1024;
  function uploadArchives(list) {
    if (!requireAuth()) return;
    var files = Array.prototype.slice.call(list || []);
    if (!files.length) return;
    if (window._lncrawlIsAdmin) {
      var big = files.filter(function (f) { return f.size > BIG_UPLOAD; });
      if (big.length) {
        files = files.filter(function (f) { return f.size <= BIG_UPLOAD; });
        big.forEach(function (f) {
          log(f.name + " is " + fmtSize(f.size) + ": too big to upload well (it would be copied twice " +
            "on the way in). Find it in the window that opened to import it where it is.", "log-info");
        });
        chooseLocalFile("Find " + big[0].name + " (" + fmtSize(big[0].size) + ") and click it: " +
          "it is imported where it is, with nothing copied.");
        if (!files.length) return;
      }
    }
    log("Uploading " + files.length + " file(s), one at a time…", "log-info");

    var failed = 0;
    (function next(i) {
      if (i >= files.length) {
        importStatus.textContent = "";
        if (failed) log(failed + " of " + files.length + " upload(s) failed — drop those again to retry.", "log-err");
        return;
      }
      uploadOne(files[i], i, files.length, function (ok) {
        if (!ok) failed += 1;
        next(i + 1);
      });
    })(0);
  }

  function uploadOne(file, index, total, done) {
    var label = (total > 1 ? "(" + (index + 1) + "/" + total + ") " : "") + file.name;
    var form = new FormData();
    form.append("files", file, file.name);

    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/import");
    xhr.setRequestHeader("Authorization", "Bearer " + token());
    // Let the browser set the multipart content type: it has to carry the
    // boundary, and naming it here without one makes the body unparseable.
    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      importStatus.textContent = "Uploading " + label + "… " + Math.round((e.loaded / e.total) * 100) + "%";
    };
    xhr.onload = function () {
      var res = null;
      try { res = JSON.parse(xhr.responseText || "null"); } catch (err) { res = null; }
      if (xhr.status < 200 || xhr.status >= 300) {
        var msg = (res && (res.error || res.detail || res.message)) || ("HTTP " + xhr.status);
        log("Upload failed for " + file.name + ": " + msg, "log-err");
        done(false);
        return;
      }
      (res || []).forEach(function (job) {
        log("Importing " + file.name + "…", "log-info");
        pollImport(job.id, file.name, 0);
      });
      done(true);
    };
    xhr.onerror = function () {
      log("Upload failed for " + file.name + " — the connection dropped.", "log-err");
      done(false);
    };
    xhr.send(form);
  }

  function loadLibrary(userId) {
    var box = document.getElementById("library-list");
    var q = "/jobs?type=53&status=2&limit=25" + (userId ? "&user_id=" + encodeURIComponent(userId) : "");
    api(q, { method: "GET" })
      .then(function (res) {
        var items = ((res && res.items) || []).filter(function (j) {
          return j.extra && j.extra.export_file;
        });
        if (!items.length) { box.innerHTML = "<span class='muted'>No exports yet.</span>"; return; }
        box.innerHTML = "";
        items.forEach(function (j) {
          var ex = j.extra || {};
          var row = document.createElement("div");
          row.className = "libitem";
          var left = document.createElement("a");
          left.textContent = (ex.domain || "export") +
            "  ·  " + (ex.exported || 0) + " novels" +
            (ex.export_size ? "  ·  " + fmtSize(ex.export_size) : "");
          left.addEventListener("click", function () { downloadBlob(j.id, ex.export_name); });
          var right = document.createElement("span");
          right.className = "muted";
          right.textContent = "↓ download";
          right.style.cursor = "pointer";
          right.addEventListener("click", function () { downloadBlob(j.id, ex.export_name); });
          row.appendChild(left); row.appendChild(right);
          box.appendChild(row);
        });
      })
      .catch(function () { box.innerHTML = "<span class='muted'>Couldn't load exports.</span>"; });
  }
  // ---- the library ZIP: made in the background, followed here ----
  var zipTimer = null, zipSeen = null;
  function zipLine(z) {
    if (z.phase === "making") {
      var pm = z.total ? Math.floor(100 * z.done / z.total) : 100;
      return "Making e-books for the novels that have none: " + count(z.done) + " of " + count(z.total) +
        " (" + pm + "%)" + (z.title ? ": " + z.title : "") + "…";
    }
    if (z.phase === "finding") return "Finding the books in your library…";
    if (z.phase === "reading") {
      // a past download's ZIP is one of the count, and holds hundreds of books
      var inside = z.inner_total ? " Now inside " + z.inner_name + ": book " + count(z.inner_done + 1) +
        " of " + count(z.inner_total) + "." : "";
      return "Checking the books for duplicates: " + count(z.done) + " of " + count(z.total) + "…" + inside;
    }
    var pct = z.size_total ? Math.floor(100 * z.size_done / z.size_total) : 0;
    return "Writing the ZIP: " + count(z.done) + " of " + count(z.total) + " books, " +
      (fmtSize(z.size_done) || "0 MB") + " of " + (fmtSize(z.size_total) || "0 MB") + " (" + pct + "%)…";
  }
  function leftOutNote(n) {
    return n ? " " + n + " partly downloaded novel(s) left out (they are in your library)." : "";
  }
  function showZip(z) {
    var line = document.getElementById("library-zip-status");
    var running = z.state === "running";
    document.getElementById("library-export").disabled = running;
    document.getElementById("library-zip-stop").classList.toggle("hidden", !running);
    line.classList.toggle("hidden", !running);
    if (running) line.textContent = zipLine(z);
    if (running && !zipTimer) zipTimer = setInterval(function () { if (!document.hidden) loadZip(); }, 1500);
    if (!running && zipTimer) { clearInterval(zipTimer); zipTimer = null; }
    // say how it ended, once, for a ZIP this page saw being made
    if (zipSeen === z.started_at && !running) {
      zipSeen = null;
      if (z.state === "done") {
        var exports = !document.getElementById("library-savedir").value.trim() && z.file && /[\\/]exports[\\/]/.test(z.file);
        if (z.unreadable) {
          log(z.unreadable + " book(s) could not be opened and are NOT in the ZIP (in use by a download, or their file is gone): " +
            (z.unreadable_titles || []).join("; ") + ". Make the ZIP again when nothing is downloading.", "log-err");
        }
        log("Library ZIP made: " + z.books + " book(s), " + fmtSize(z.size) + "." + leftOutNote(z.left_out) +
          (exports ? " Downloading it now." : " Saved to " + z.file + ".") + " Extract it and open index.html to browse.", "log-ok");
        if (exports) {
          fetch("/api/admin/library/zip/file", { headers: { "Authorization": "Bearer " + token() } })
            .then(function (res) { if (!res.ok) throw new Error("HTTP " + res.status); return res.blob(); })
            .then(function (blob) {
              var url = URL.createObjectURL(blob);
              var link = document.createElement("a");
              link.href = url; link.download = "library.zip";
              document.body.appendChild(link); link.click(); link.remove();
              setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
            })
            .catch(function (e) { log("Could not download the ZIP: " + e.message + ". It is in " + z.file + ".", "log-err"); });
        }
      } else if (z.state === "stopped") {
        log("Library ZIP stopped. Nothing was kept of it.", "log-info");
      } else if (z.state === "failed") {
        log("Library ZIP failed: " + (z.error || "unknown error"), "log-err");
      }
    }
    if (running) zipSeen = z.started_at;
  }
  function loadZip() {
    api("/admin/library/zip", { method: "GET" }).then(showZip).catch(function () {});
  }
  document.getElementById("library-export").addEventListener("click", function () {
    if (!requireAuth()) return;
    var body = {
      save_dir: document.getElementById("library-savedir").value.trim() || null,
      nsfw: document.getElementById("library-nsfw").value,
      complete_only: document.getElementById("library-completeonly").checked
    };
    log("Packing all saved books and an offline index… progress shows under the button.", "log-info");
    api("/admin/library/zip", { method: "POST", body: JSON.stringify(body) })
      .then(function (z) { zipSeen = z.started_at; showZip(z); })
      .catch(function (error) { log("Library ZIP failed: " + error.message, "log-err"); });
  });
  document.getElementById("library-zip-stop").addEventListener("click", function () {
    api("/admin/library/zip", { method: "DELETE" }).then(showZip).catch(function () {});
  });

  document.getElementById("library-refresh").addEventListener("click", function () {
    if (!requireAuth()) return;
    loadLibrary(window._lncrawlUserId);
  });

  // ---- Storage, backup & phone ----
  // What uses the space is worked out only when asked (going through every
  // novel's folder takes minutes on a big library), then kept and shown.
  function loadStorage(measure) {
    api("/novels/storage" + (measure ? "?measure=true" : ""), { method: "GET" }).then(function (s) {
      var line = document.getElementById("storage-line");
      var free = s.free != null ? (fmtSize(s.free) || "0 MB") + " free of " + fmtSize(s.disk_total) + " on the library's drive" : "";
      if (s.novel_count != null) {
        line.textContent = (s.novel_count || 0) + " novel(s) use " + (fmtSize(s.total || 0) || "0 MB") +
          " · export zips use " + (fmtSize(s.exports || 0) || "0 MB") + (free ? " · " + free : "") +
          (s.measured_at ? " (measured " + new Date(s.measured_at * 1000).toLocaleString() + ")" : "");
      } else {
        line.textContent = (free || "") + (s.measuring ? "" : (free ? ". " : "") + "Press “Check what uses the space” to measure the novels.");
      }
      if (s.measuring) {
        line.textContent += " Measuring now: this can take a while on a big library…";
        setTimeout(function () { loadStorage(false); }, 4000);
      }
      var box = document.getElementById("storage-largest");
      box.innerHTML = "";
      (s.largest || []).slice(0, 8).forEach(function (n) {
        var d = document.createElement("div");
        d.textContent = fmtSize(n.size) + "  ·  " + n.title;
        box.appendChild(d);
      });
    }).catch(function (e) {
      document.getElementById("storage-line").textContent = "Couldn't check usage: " + e.message;
    });
  }
  // ---- Move the library to another drive ----
  var moveTimer = null;
  function count(n) { return (n || 0).toLocaleString(); }
  var lastMoveState = null, rescueShown = false;
  function showMove(s) {
    if (s.rescued && !rescueShown) {
      rescueShown = true;
      log("⚠ " + s.rescued, "log-err");
    }
    var old = s.old_copy;
    document.getElementById("space-notice").classList.toggle("hidden", !s.low);
    if (s.low) {
      document.getElementById("space-title").textContent = "Only " + (fmtSize(s.free) || "0 MB") +
        " is free on the drive the library is on (" + s.folder + ").";
    }
    var line = document.getElementById("move-status");
    var busyNow = ["pausing", "measuring", "copying", "finishing", "switching"].indexOf(s.state) >= 0;
    var wasMoving = ["pausing", "measuring", "copying", "finishing", "switching"].indexOf(lastMoveState) >= 0;
    if (wasMoving && s.state === "idle" && old && old.path) {
      log("✓ The library is now in " + s.folder + ". Removing the old copy from " + old.path + ".", "log-ok");
      loadPlaces();
    }
    lastMoveState = s.state;
    var text;
    if (s.state === "pausing") {
      text = "Pausing downloads before the copy starts…";
    } else if (s.state === "measuring") {
      text = "Measuring the library: " + count(s.files) + " files, " + (fmtSize(s.bytes) || "0 MB") + " so far…";
    } else if (s.state === "copying") {
      var pct = s.total_bytes ? Math.floor(100 * s.bytes / s.total_bytes) : 0;
      text = "Copying to " + s.target + ": " + pct + "% (" + (fmtSize(s.bytes) || "0 MB") + " of " +
        fmtSize(s.total_bytes) + ", " + count(s.files) + " of " + count(s.total_files) +
        " files). Downloads are paused; the library is still in use from " + s.folder + ".";
      if (s.problem) {
        text = "⏳ " + s.target + " is not answering (" + s.problem + "). Waiting and trying again by " +
          "itself for up to 15 minutes; check the drive is still connected. " + text;
      }
    } else if (s.state === "finishing") {
      text = "Almost done: " + (s.step || "finishing the copy") + "… The library is still in use from " +
        s.folder + ".";
    } else if (s.state === "switching") {
      text = "Switching over to " + s.target + ". The app may stop answering for a few seconds.";
    } else if (s.state === "copied") {
      text = "✓ Copied to " + s.target + ", but the app could not switch over while it was running " +
        "(something kept the database busy). Close the app and open it again to finish: it then runs " +
        "from there and removes the old copy from " + s.folder + ". Downloads stay paused until then.";
    } else if (s.state === "chosen") {
      text = "✓ Close the app and open it again: it then runs from the library in " + s.target +
        ". Nothing was copied, and the library in " + s.folder + " stays where it is.";
    } else if (s.state === "failed") {
      text = "The move did not happen: " + (s.error || "unknown error") + ". The library is unchanged in " +
        s.folder + ". Click Move library again to carry on from what was already copied.";
    } else if (old && old.state === "removing") {
      text = "The library is now in " + s.folder + ". Removing the old copy from " + old.path + ": " +
        (fmtSize(old.freed) || "0 MB") + " freed so far…";
    } else if (old && old.state === "done") {
      text = "The library is now in " + s.folder + ". The old copy is gone from " + old.path +
        " (" + (fmtSize(old.freed) || "0 MB") + " freed" +
        (old.left ? "; " + count(old.left) + " file(s) the new folder does not have were left there" : "") +
        (old.in_use ? "; " + count(old.in_use) + " file(s) still in use are removed the next time the app starts" : "") +
        ").";
    } else if (old && old.state === "failed") {
      text = "The library is now in " + s.folder + ". The old copy in " + old.path +
        " could not be removed (" + (old.error || "unknown error") + "); it is safe to delete by hand.";
    } else {
      text = "The library is in " + s.folder + (s.free != null ? " (" + (fmtSize(s.free) || "0 MB") +
        " free on that drive)" : "") + ".";
    }
    if (s.left_behind && s.state === "idle") {
      text += " " + count(s.left_behind) + " file(s) another program was holding open (the app window's " +
        "browser profile, logs) were not brought along; they are made again as needed.";
    }
    line.textContent = text;
    var waiting = s.state === "copied" || s.state === "chosen";
    document.getElementById("move-btn").disabled = busyNow || waiting;
    document.getElementById("move-use").disabled = busyNow || waiting;
    document.getElementById("move-cancel").classList.toggle("hidden", !(busyNow || waiting));
    document.querySelectorAll(".place button, #found-use").forEach(function (button) {
      button.disabled = busyNow || waiting;
    });
    var watching = busyNow || (old && old.state === "removing");
    if (watching && !moveTimer) moveTimer = setInterval(function () { if (!document.hidden) loadMove(); }, 2000);
    if (!watching && moveTimer) { clearInterval(moveTimer); moveTimer = null; }
  }
  // ---- a folder picker for every folder box: walk the drives instead of typing ----
  var picker = document.getElementById("folder-picker");
  var pickerFor = null, pickerAt = null;
  // choosing a file instead of a folder: which kinds, and what to do with it
  var pickerFiles = null, pickerOnFile = null;
  function pickerMode(files, onFile, title) {
    pickerFiles = files; pickerOnFile = onFile;
    document.getElementById("picker-title").textContent = title || "Choose a folder";
    document.getElementById("picker-use").classList.toggle("hidden", !!files);
    document.getElementById("picker-new-btn").classList.toggle("hidden", !!files);
  }
  function pickerRow(glyph, label, meta, tag, open) {
    var b = document.createElement("button");
    b.type = "button"; b.setAttribute("role", "listitem");
    var g = document.createElement("span"); g.className = "glyph"; g.textContent = glyph; g.setAttribute("aria-hidden", "true");
    var l = document.createElement("span"); l.className = "label"; l.textContent = label;
    b.appendChild(g); b.appendChild(l);
    if (tag) { var t = document.createElement("span"); t.className = "tag"; t.textContent = tag; b.appendChild(t); }
    if (meta) { var m = document.createElement("span"); m.className = "meta"; m.textContent = meta; b.appendChild(m); }
    b.addEventListener("click", open);
    return b;
  }
  var BS = String.fromCharCode(92);  // a backslash, without escaping it twice over
  function pickerTrail(path) {
    var trail = document.getElementById("picker-trail");
    trail.innerHTML = "";
    var top = document.createElement("button");
    top.type = "button"; top.textContent = "This computer";
    top.addEventListener("click", function () { pickerOpen(null); });
    if (!path) top.setAttribute("aria-current", "location");
    trail.appendChild(top);
    if (!path) return;
    var windows = /^[A-Za-z]:[\\\\/]/.test(path);
    var sep = windows ? BS : "/";
    var parts = path.split(/[\\\\/]+/).filter(function (x) { return x; });
    var built = windows ? "" : "/";
    parts.forEach(function (part, i) {
      built = windows ? (i === 0 ? part + BS : built + (built.slice(-1) === BS ? "" : BS) + part)
                      : built + (built.slice(-1) === "/" ? "" : "/") + part;
      var s = document.createElement("span"); s.className = "sep"; s.textContent = "›"; trail.appendChild(s);
      var b = document.createElement("button"); b.type = "button"; b.textContent = part;
      var target = built;
      b.addEventListener("click", function () { pickerOpen(target); });
      if (i === parts.length - 1) b.setAttribute("aria-current", "location");
      trail.appendChild(b);
    });
    if (!parts.length) {  // the root folder "/"
      var s0 = document.createElement("span"); s0.className = "sep"; s0.textContent = "›"; trail.appendChild(s0);
      var r = document.createElement("button"); r.type = "button"; r.textContent = sep; r.setAttribute("aria-current", "location");
      trail.appendChild(r);
    }
  }
  function pickerOpen(path) {
    var list = document.getElementById("picker-list");
    var note = document.getElementById("picker-note");
    list.innerHTML = "<div class='empty'>Looking…</div>";
    var query = "?path=" + encodeURIComponent(path || "") +
      (pickerFiles ? "&files=" + encodeURIComponent(pickerFiles) : "");
    api("/admin/folders" + query, { method: "GET" }).then(function (r) {
      pickerAt = r.path;
      pickerTrail(r.path);
      list.innerHTML = "";
      document.getElementById("picker-use").disabled = !r.path;
      document.getElementById("picker-new-btn").disabled = !r.path;
      if (!r.path) {
        note.textContent = "Pick a drive or one of your folders to look in.";
        (r.places || []).forEach(function (p) {
          var meta = p.free != null ? (fmtSize(p.free) || "0 MB") + " free" : "";
          list.appendChild(pickerRow(p.kind === "drive" ? "◼" : "▸", p.name, meta, "", function () { pickerOpen(p.path); }));
        });
        return;
      }
      note.textContent = (r.library ? "This folder holds a library. " : "") +
        (r.free != null ? (fmtSize(r.free) || "0 MB") + " free on this drive." : "");
      if (r.parent) list.appendChild(pickerRow("↰", "Up one level", "", "", function () { pickerOpen(r.parent); }));
      r.folders.forEach(function (f) {
        list.appendChild(pickerRow("▸", f.name, "", f.library ? "library" : "", function () { pickerOpen(f.path); }));
      });
      (r.files || []).forEach(function (f) {
        list.appendChild(pickerRow("▤", f.name, fmtSize(f.size) || "0 MB", "", function () {
          picker.close();
          if (pickerOnFile) pickerOnFile(f.path, f.size);
        }));
      });
      if (!r.folders.length && !(r.files || []).length) {
        var e = document.createElement("div"); e.className = "empty";
        e.textContent = pickerFiles ? "No folders or files to import in here."
          : "No folders in here. Use this one, or make a new folder.";
        list.appendChild(e);
      }
      if (r.more) {
        var m = document.createElement("div"); m.className = "empty";
        m.textContent = count(r.more) + " more folders are not listed. Type the path in the box instead.";
        list.appendChild(m);
      }
      var first = list.querySelector("button");
      if (first) first.focus();
    }).catch(function (e) {
      note.textContent = e.message;
      list.innerHTML = "";
      if (pickerAt !== path) {
        list.appendChild(pickerRow("↰", "Back", "", "", function () { pickerOpen(pickerAt); }));
      }
    });
  }
  document.querySelectorAll("button.browse").forEach(function (button) {
    button.addEventListener("click", function () {
      if (!requireAuth()) return;
      pickerFor = document.getElementById(button.getAttribute("data-for"));
      pickerMode(null, null, "Choose a folder");
      document.getElementById("picker-new").classList.add("hidden");
      pickerAt = null;
      if (typeof picker.showModal === "function") picker.showModal(); else picker.setAttribute("open", "");
      pickerOpen(pickerFor.value.trim() || null);
    });
  });
  // Import a file where it is on this computer: nothing uploaded or copied.
  var IMPORTABLE = ".zip,.epub,.cbz,.pdf,.docx,.mobi,.azw3,.fb2,.rtf,.txt";
  function importLocal(path, size) {
    var name = path.split(BS).join("/").split("/").pop();
    api("/admin/import-file", { method: "POST", body: JSON.stringify({ path: path }) })
      .then(function (job) {
        log("Importing " + name + " (" + fmtSize(size) + ") from where it is…", "log-info");
        pollImport(job.id, name, 0);
      })
      .catch(function (e) { log("Could not import " + name + ": " + e.message, "log-err"); });
  }
  function chooseLocalFile(note) {
    if (!requireAuth()) return;
    pickerMode(IMPORTABLE, importLocal, "Choose a file to import");
    document.getElementById("picker-new").classList.add("hidden");
    pickerAt = null;
    if (typeof picker.showModal === "function") picker.showModal(); else picker.setAttribute("open", "");
    pickerOpen(null);
    if (note) document.getElementById("picker-note").textContent = note;
  }
  document.getElementById("import-local").addEventListener("click", function () { chooseLocalFile(); });

  document.getElementById("picker-use").addEventListener("click", function () {
    if (pickerFor && pickerAt) {
      pickerFor.value = pickerAt;
      pickerFor.dispatchEvent(new Event("change"));
    }
    picker.close();
  });
  document.getElementById("picker-new-btn").addEventListener("click", function () {
    var row = document.getElementById("picker-new");
    row.classList.remove("hidden");
    document.getElementById("picker-name").value = "";
    document.getElementById("picker-name").focus();
  });
  function pickerMake() {
    var name = document.getElementById("picker-name").value.trim();
    if (!name || !pickerAt) return;
    api("/admin/folders", { method: "POST", body: JSON.stringify({ path: pickerAt, name: name }) })
      .then(function (r) {
        document.getElementById("picker-new").classList.add("hidden");
        pickerOpen(r.path);
      })
      .catch(function (e) { document.getElementById("picker-note").textContent = e.message; });
  }
  document.getElementById("picker-make").addEventListener("click", pickerMake);
  document.getElementById("picker-name").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); pickerMake(); }
  });

  // ---- drives and libraries to pick from, so no folder has to be typed ----
  function placeRow(name, about, label, act, share) {
    var row = document.createElement("div"); row.className = "place";
    var text = document.createElement("div"); text.className = "grow";
    var title = document.createElement("div"); title.className = "name"; title.textContent = name;
    var note = document.createElement("div"); note.className = "about"; note.textContent = about;
    text.appendChild(title); text.appendChild(note);
    if (share != null) {
      var bar = document.createElement("div"); bar.className = "bar";
      var fill = document.createElement("i"); fill.style.width = Math.round(100 * share) + "%";
      bar.appendChild(fill); text.appendChild(bar);
    }
    row.appendChild(text);
    if (label) {
      var button = document.createElement("button");
      button.textContent = label; button.className = "secondary";
      button.addEventListener("click", act);
      row.appendChild(button);
    }
    return row;
  }
  function useLibrary(folder) {
    document.getElementById("move-folder").value = folder;
    document.getElementById("move-use").click();
  }
  function moveTo(folder) {
    document.getElementById("move-folder").value = folder;
    document.getElementById("move-btn").click();
  }
  function loadPlaces() {
    if (!token()) return;
    api("/admin/library/places", { method: "GET" }).then(function (p) {
      var libraries = p.libraries || [], drives = p.drives || [];
      var found = document.getElementById("found-list");
      found.innerHTML = "";
      libraries.forEach(function (lib) {
        var when = lib.last_used ? "Last used " + new Date(lib.last_used * 1000).toLocaleDateString() : "A library";
        found.appendChild(placeRow(lib.path, when, "Open this library", function () { useLibrary(lib.path); }));
      });
      document.getElementById("found-box").classList.toggle("hidden", !libraries.length);
      // a library with nothing in it, and a full one on another drive: say so everywhere
      var offer = libraries.length && p.empty;
      document.getElementById("library-dot").classList.toggle("hidden", !offer);
      document.getElementById("found-notice").classList.toggle("hidden", !offer);
      if (offer) {
        document.getElementById("found-title").textContent = "The library here has no novels in it.";
        document.getElementById("found-text").textContent = "There is a library in " + libraries[0].path + ".";
        document.getElementById("found-use").onclick = function () { useLibrary(libraries[0].path); };
      }
      var list = document.getElementById("drive-list");
      list.innerHTML = "";
      drives.forEach(function (drive) {
        var about = (fmtSize(drive.free) || "0 MB") + " free of " + fmtSize(drive.total);
        var used = drive.total ? 1 - drive.free / drive.total : null;
        if (drive.current) {
          list.appendChild(placeRow(drive.path, about + ". The library is on this drive now.", "", null, used));
        } else {
          list.appendChild(placeRow(drive.path, about, "Move the library here", function () { moveTo(drive.path); }, used));
        }
      });
      if (!drives.length) {
        list.innerHTML = "<span class='muted'>No drives were found to pick from. Type a folder below.</span>";
      }
    }).catch(function () {
      document.getElementById("drive-list").innerHTML =
        "<span class='muted'>Couldn't look at your drives. Type a folder below.</span>";
    });
  }
  // ---- books on a drive of their own ----
  function showBooks(b) {
    var box = document.getElementById("books-box");
    var split = document.getElementById("books-split"), make = document.getElementById("books-make");
    var on = document.getElementById("books-on");
    var away = b.library !== b.base;  // the library itself is on another drive
    split.classList.toggle("hidden", b.configured || !away || !!b.split_to);
    make.classList.toggle("hidden", b.configured || away || !!b.split_to);
    on.classList.toggle("hidden", !b.configured);
    var here = document.getElementById("books-here");
    if (!here.value) here.value = b.base;
    var m = b.moving || {};
    box.innerHTML = "";
    var p = document.createElement("p"); p.className = "hint"; box.appendChild(p);
    if (b.split_to) {
      p.textContent = "Close the app and open it again: it then copies the database, settings and covers to " +
        b.split_to + " (a few minutes), leaves the books in " + b.library + ", and runs from there.";
      var undo = document.createElement("button"); undo.className = "secondary"; undo.textContent = "Don't split";
      undo.addEventListener("click", function () {
        api("/books/split", { method: "DELETE" }).then(showBooks).catch(function (e) { log(e.message, "log-err"); });
      });
      box.appendChild(undo);
      return;
    }
    if (!b.configured) {
      p.textContent = "All of the library is in " + b.library + "." + (b.split_failed
        ? " The last try at keeping it on this computer did not finish, so it carried on from there: " +
          b.split_failed + ". Each step it got through is in " + b.library + String.fromCharCode(92) + "move.log."
        : "");
      return;
    }
    var line = (b.plugged ? "The books are in " + b.folder + (b.free != null ? " (" + fmtSize(b.free) + " free)" : "")
      : "The drive with the books (" + b.folder + ") is not plugged in: what is on it can't be read until it is.") +
      " " + b.on_drive.length + " novel(s) there; the rest are on this computer.";
    if (m.running) line += " Moving " + m.done + " of " + m.total + " to " + (m.to === "drive" ? "the drive" : "this computer") + "…";
    else if (m.total) line += " Last move: " + (m.done - m.failed) + " of " + m.total + " moved" + (m.failed ? " (" + m.failed + " failed: " + m.error + ")" : "") + ".";
    p.textContent = line;
    document.getElementById("books-new").value = b.new || "drive";
    document.getElementById("books-all-drive").disabled = !b.plugged || !!m.running;
    document.getElementById("books-all-here").disabled = !b.plugged || !!m.running;
    if (m.running) setTimeout(loadBooks, 2000);
  }
  function loadBooks() {
    api("/books", { method: "GET" }).then(showBooks).catch(function () {});
  }
  document.getElementById("books-split-btn").addEventListener("click", function () {
    if (!requireAuth()) return;
    var folder = document.getElementById("books-here").value.trim();
    if (!folder) { log("Choose a folder on this computer for the library.", "log-err"); return; }
    if (!confirm("Keep the library in " + folder + " and the books where they are?\\n\\nIt is done when you next " +
        "open the app: the database, settings and covers are copied (a few minutes), nothing is removed from the drive.")) return;
    api("/books/split", { method: "POST", body: JSON.stringify({ folder: folder }) })
      .then(function (b) { log("Close the app and open it again to finish.", "log-ok"); showBooks(b); })
      .catch(function (e) { log("Could not split the library: " + e.message, "log-err"); });
  });
  document.getElementById("books-make-btn").addEventListener("click", function () {
    if (!requireAuth()) return;
    var folder = document.getElementById("books-folder").value.trim();
    if (!folder) { log("Choose a folder on the drive for the books.", "log-err"); return; }
    api("/books/drive", { method: "POST", body: JSON.stringify({ folder: folder }) })
      .then(function (b) { log("Books can go to " + b.folder + " now.", "log-ok"); showBooks(b); })
      .catch(function (e) { log("Could not use that folder: " + e.message, "log-err"); });
  });
  document.getElementById("books-new").addEventListener("change", function () {
    api("/books/new", { method: "POST", body: JSON.stringify({ to: this.value }) })
      .then(showBooks).catch(function (e) { log(e.message, "log-err"); });
  });
  function moveAll(to) {
    if (!requireAuth()) return;
    if (!confirm("Move every novel's books to " + (to === "drive" ? "the drive" : "this computer") +
        "? It runs in the background and can take hours for a big library; novels can be read the whole time.")) return;
    api("/books/move", { method: "POST", body: JSON.stringify({ ids: [], all: true, to: to }) })
      .then(showBooks).catch(function (e) { log("Could not move the books: " + e.message, "log-err"); });
  }
  document.getElementById("books-all-drive").addEventListener("click", function () { moveAll("drive"); });
  document.getElementById("books-all-here").addEventListener("click", function () { moveAll("here"); });

  function loadMove() {
    api("/admin/library/location", { method: "GET" }).then(showMove).catch(function () {});
  }
  document.getElementById("move-btn").addEventListener("click", function () {
    if (!requireAuth()) return;
    var folder = document.getElementById("move-folder").value.trim();
    if (!folder) { log("Type the folder to move the library to, for example D:\\\\LNCrawl.", "log-err"); return; }
    if (!confirm("Move the whole library to " + folder + "?\\n\\nDownloads pause while it copies (a big " +
        "library can take hours); you can keep reading meanwhile. When the copy is done the app " +
        "switches over by itself and removes the old copy.")) return;
    api("/admin/library/move", { method: "POST", body: JSON.stringify({ folder: folder }) })
      .then(function (s) { log("Moving the library to " + s.target + "…", "log-info"); showMove(s); })
      .catch(function (e) { log("Could not start the move: " + e.message, "log-err"); });
  });
  document.getElementById("move-use").addEventListener("click", function () {
    if (!requireAuth()) return;
    var folder = document.getElementById("move-folder").value.trim();
    if (!folder) { log("Type the folder the library is in, for example F:\\\\Novels.", "log-err"); return; }
    if (!confirm("Use the library that is already in " + folder + "?\\n\\nNothing is copied. After you " +
        "close the app and open it again it runs from there, and the library it uses now is left " +
        "where it is.")) return;
    api("/admin/library/use", { method: "POST", body: JSON.stringify({ folder: folder }) })
      .then(function (s) { log("The next start uses the library in " + s.target + ".", "log-ok"); showMove(s); })
      .catch(function (e) { log("Could not use that folder: " + e.message, "log-err"); });
  });
  document.getElementById("move-cancel").addEventListener("click", function () {
    if (!requireAuth()) return;
    api("/admin/library/move", { method: "DELETE" })
      .then(function (s) { log("Move cancelled. The library stays where it is.", "log-info"); showMove(s); loadMove(); })
      .catch(function (e) { log("Could not cancel the move: " + e.message, "log-err"); });
  });

  document.getElementById("storage-refresh").addEventListener("click", function () {
    if (!requireAuth()) return;
    loadStorage(true);
  });
  document.getElementById("storage-clean").addEventListener("click", function () {
    if (!requireAuth()) return;
    if (!confirm("Delete all generated export ZIPs? Your downloaded novels are not touched; backups are kept.")) return;
    api("/novels/storage/exports", { method: "DELETE" }).then(function (r) {
      log("Removed " + r.removed + " zip(s), freed " + (fmtSize(r.freed) || "0 MB") + ".", "log-ok");
      loadStorage();
    }).catch(function (e) { log("Cleanup failed: " + e.message, "log-err"); });
  });
  // ---- Download one novel ----
  document.getElementById("one-start").addEventListener("click", function () {
    if (!requireAuth()) return;
    var urls = document.getElementById("one-urls").value.split(/[\\s,]+/).map(function (u) {
      return u.trim();
    }).filter(function (u) { return /^https?:[/][/]/i.test(u); });
    if (!urls.length) { log("Paste a novel's link first (it starts with https://).", "log-err"); return; }
    var home = urls.filter(function (u) { return /^https?:[/][/][^/]+[/]?$/i.test(u); });
    if (home.length) {
      log("That is a site's home page, not a novel: " + home[0] + ". Open the novel on the site and copy its link, or use Download a whole site below.", "log-err");
      return;
    }
    var btn = this; busy(btn, true);
    api("/job/create/bulk-download", { method: "POST", body: JSON.stringify({ urls: urls }) })
      .then(function () {
        log("Downloading " + urls.length + " novel(s) into your library. It carries on by itself; follow it on the Downloads page, and read it in the Reader as chapters arrive.", "log-ok");
        document.getElementById("one-urls").value = "";
      })
      .catch(function (e) { log("Could not start the download: " + e.message, "log-err"); })
      .finally(function () { busy(btn, false); });
  });

  // ---- Clean up extras ----
  function esc(text) {
    var d = document.createElement("div"); d.textContent = text == null ? "" : String(text); return d.innerHTML;
  }
  function showExtras(r) {
    var box = document.getElementById("extras-box");
    box.classList.remove("hidden");
    var d = r.duplicates, e = r.ebooks, l = r.leftovers;
    document.getElementById("extras-duplicates-text").textContent = d.sets
      ? "Duplicate novels: " + d.sets + " title(s) saved more than once (" + d.copies + " extra cop" + (d.copies === 1 ? "y" : "ies") + ")"
      : "Duplicate novels: none found";
    document.getElementById("extras-duplicates").disabled = !d.sets;
    document.getElementById("extras-duplicates").checked = !!d.sets;
    document.getElementById("extras-duplicates-list").innerHTML = d.list.map(function (copies) {
      return "<div><b>" + esc(copies[0].title) + "</b>: " + copies.map(function (c) {
        return esc(c.domain) + " (" + c.downloaded + "/" + c.chapters + ")";
      }).join(", ") + "</div>";
    }).join("");
    var ebooks = e.records + e.files;
    document.getElementById("extras-ebooks-text").textContent = ebooks
      ? "Extra e-book copies: " + e.records + " old record(s), " + e.files + " file(s), " + (fmtSize(e.size) || "0 MB")
      : "Extra e-book copies: none found";
    document.getElementById("extras-ebooks").disabled = !ebooks;
    document.getElementById("extras-ebooks").checked = !!ebooks;
    document.getElementById("extras-leftovers-text").textContent = l.count
      ? "Leftover files: " + l.count + " item(s), " + (fmtSize(l.size) || "0 MB")
      : "Leftover files: none found";
    document.getElementById("extras-leftovers").disabled = !l.count;
    document.getElementById("extras-leftovers").checked = !!l.count;
    document.getElementById("extras-leftovers-list").innerHTML = l.list.map(function (item) {
      return "<div>" + esc(item.path) + " (" + esc(item.why) + ", " + (fmtSize(item.size) || "0 MB") + ")</div>";
    }).join("");
    document.getElementById("extras-go").disabled = !(d.sets || ebooks || l.count);
    if (r.cleaning && r.cleaning.running) watchExtras();
  }
  var extrasTimer = null;
  function showExtrasProgress(s) {
    var wrap = document.getElementById("extras-progress");
    wrap.classList.remove("hidden");
    var bar = document.getElementById("extras-bar");
    bar.max = Math.max(1, s.total); bar.value = s.done;
    var text = s.running
      ? "Cleaning up: " + s.done + " of " + (s.total || "?") + (s.current ? " · " + s.current : "")
      : "Done: " + s.merged + " duplicate cop" + (s.merged === 1 ? "y" : "ies") + " merged" +
        (s.kept ? ", " + s.kept + " kept because they have chapters the other lacks" : "") +
        ", " + (fmtSize(s.freed) || "0 MB") + " freed" + (s.failed ? ", " + s.failed + " could not be cleaned (" + s.error + ")" : "") + ".";
    document.getElementById("extras-text").textContent = text;
  }
  function watchExtras() {
    if (extrasTimer) return;
    extrasTimer = setInterval(function () {
      if (document.hidden) return;
      api("/admin/extras/status").then(function (s) {
        showExtrasProgress(s);
        if (!s.running) {
          clearInterval(extrasTimer); extrasTimer = null;
          document.getElementById("extras-go").disabled = false;
          log("Extras cleaned up.", "log-ok");
          loadStorage();
        }
      }).catch(function () {});
    }, 1500);
  }
  document.getElementById("extras-find").addEventListener("click", function () {
    if (!requireAuth()) return;
    var btn = this; busy(btn, true);
    log("Looking for extras (a big library can take a minute)…", "log-info");
    api("/admin/extras").then(showExtras)
      .catch(function (e) { log("Could not look for extras: " + e.message, "log-err"); })
      .finally(function () { busy(btn, false); });
  });
  document.getElementById("extras-go").addEventListener("click", function () {
    if (!requireAuth()) return;
    var kinds = ["duplicates", "ebooks", "leftovers"].filter(function (k) {
      var box = document.getElementById("extras-" + k); return box.checked && !box.disabled;
    });
    if (!kinds.length) { log("Tick what to clean up first.", "log-err"); return; }
    if (!confirm("Clean up the ticked extras? This cannot be undone. Duplicates are merged into their most complete copy; a copy with chapters the other lacks is kept.")) return;
    this.disabled = true;
    api("/admin/extras", { method: "POST", body: JSON.stringify({ kinds: kinds }) })
      .then(function (s) { showExtrasProgress(s); watchExtras(); })
      .catch(function (e) { log("Could not clean up: " + e.message, "log-err"); document.getElementById("extras-go").disabled = false; });
  });
  document.getElementById("backup-btn").addEventListener("click", function () {
    if (!requireAuth()) return;
    var btn = this; busy(btn, true);
    log("Creating backup (database + all novels)… this can take a while for a big library.", "log-info");
    api("/admin/backup", { method: "POST" }).then(function (r) {
      log("Backup ready: " + r.file + " (" + fmtSize(r.size) + ")" +
        (r.saved_to ? " — saved to your Desktop." : " — in the exports folder."), "log-ok");
    }).catch(function (e) { log("Backup failed: " + e.message, "log-err"); })
      .finally(function () { busy(btn, false); });
  });
  document.getElementById("phone-btn").addEventListener("click", function () {
    if (!requireAuth()) return;
    var box = document.getElementById("phone-box");
    if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
    api("/settings/lan", { method: "GET" }).then(function (r) {
      document.getElementById("phone-url").textContent = r.url;
      return fetch("/api/settings/lan-qr", { headers: { "Authorization": "Bearer " + token() } });
    }).then(function (res) { if (!res.ok) throw new Error("HTTP " + res.status); return res.text(); })
      .then(function (svg) {
        document.getElementById("phone-qr").innerHTML = svg;
        box.classList.remove("hidden");
      })
      .catch(function (e) { log("Couldn't get the phone link: " + e.message, "log-err"); });
  });

  function loadNotifyConfig() {
    api("/job/notify-config", { method: "GET" })
      .then(function (cfg) {
        document.getElementById("notify-desktop").checked = !!cfg.desktop_toast;
        document.getElementById("notify-webhook").value = cfg.webhook_url || "";
      })
      .catch(function () {});
  }

  document.getElementById("notify-save").addEventListener("click", function () {
    if (!requireAuth()) return;
    var btn = this;
    var body = {
      desktop_toast: document.getElementById("notify-desktop").checked,
      webhook_url: document.getElementById("notify-webhook").value.trim()
    };
    busy(btn, true);
    api("/job/notify-config", { method: "POST", body: JSON.stringify(body) })
      .then(function (cfg) {
        var bits = [];
        if (cfg.desktop_toast) bits.push("desktop");
        if (cfg.webhook_url) bits.push("webhook");
        log("Notification settings saved" +
          (bits.length ? " — " + bits.join(" + ") + " active." : " — none active."), "log-ok");
      })
      .catch(function (e) { log("Save notification settings failed: " + e.message, "log-err"); })
      .finally(function () { busy(btn, false); });
  });

  document.getElementById("completeness-btn").addEventListener("click", function () {
    if (!requireAuth()) return;
    var novelId = document.getElementById("novel-id").value.trim();
    if (!novelId) { log("Enter a novel ID.", "log-err"); return; }
    api("/novel/" + encodeURIComponent(novelId) + "/completeness").then(function (c) {
      log((c.complete ? "Complete" : "Incomplete") + " — " + c.downloaded + "/" + c.total +
        " downloaded, " + c.missing + " missing, " + c.failed + " failed, " + c.empty + " empty.", "log-info");
      (c.gaps || []).slice(0, 20).forEach(function (gap) {
        log("#" + gap.serial + " " + gap.title + ": " + gap.status + (gap.error ? " — " + gap.error : ""), "log-info");
      });
    }).catch(function (error) { log("Completeness check failed: " + error.message, "log-err"); });
  });
  document.getElementById("retry-btn").addEventListener("click", function () {
    if (!requireAuth()) return;
    var btn = this;
    var novelId = document.getElementById("novel-id").value.trim();
    if (!novelId) { log("Enter a novel ID.", "log-err"); return; }
    busy(btn, true);
    log("Queuing missing-chapter retry for novel " + novelId + " …", "log-info");
    api("/job/create/fetch-missing", { method: "POST", body: JSON.stringify({ novel_id: novelId }) })
      .then(function (job) {
        log("Retry job created: " + job.id, "log-ok");
        pollJob(job.id, "Retry missing");
      })
      .catch(function (e) { log("Retry failed: " + e.message, "log-err"); })
      .finally(function () { busy(btn, false); });
  });

  refreshAuthUI();
  document.getElementById("space-go").addEventListener("click", function () {
    showPane("library"); window.scrollTo(0, 0);
  });
  // the free space is looked at again every minute, whichever screen is open
  setInterval(function () { if (token() && !moveTimer && !document.hidden) loadMove(); }, 60000);
  // a page shown again catches up at once instead of at its next tick
  document.addEventListener("visibilitychange", function () {
    if (document.hidden || !token()) return;
    if (zipTimer) loadZip();
    if (moveTimer) loadMove();
  });
  showPane((location.hash || "").replace("#", ""));
})();
</script>
</body>
</html>
"""


@router.get("/tools", include_in_schema=False)
async def tools_page(request: Request) -> Response:
    from ..context import ctx

    info = f"Lightnovel Crawler v{ctx.config.app.version} · build {ctx.config.app.build}"
    # Encoded and gzipped once per build info; a revalidating browser gets a 304.
    page = text_blob(_TOOLS_HTML.replace("__BUILD_INFO__", info), "text/html; charset=utf-8")
    return blob_response(request, page, {"Cache-Control": "no-cache"})
