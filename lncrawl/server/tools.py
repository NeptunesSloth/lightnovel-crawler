"""Standalone "Tools" page.

A self-contained, dependency-free admin/power-user page that exposes a few
job actions directly over the REST API. It is intentionally kept outside of
``lncrawl/server/web`` (which is overwritten by the web-sync workflow from the
``lncrawl-web`` artifacts) so it survives front-end rebuilds. The markup is
embedded as a string constant so it is always packaged with the wheel/exe.

Served at ``GET /tools``.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

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
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #0f1115; color: #e6e6e6; line-height: 1.5;
  }
  .wrap { max-width: 720px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: #8b93a1; margin: 0 0 20px; font-size: 14px; }
  .card {
    background: #171a21; border: 1px solid #262b36; border-radius: 12px;
    padding: 18px; margin-bottom: 16px;
  }
  .card h2 { font-size: 16px; margin: 0 0 4px; }
  .card p.hint { color: #8b93a1; font-size: 13px; margin: 0 0 14px; }
  details { margin-top: 12px; border-top: 1px solid #20242d; padding-top: 8px; }
  details > summary {
    cursor: pointer; color: #aab2c0; font-size: 13px; list-style: none;
    user-select: none; padding: 4px 0;
  }
  details > summary::before { content: "▸ "; color: #6b7280; }
  details[open] > summary::before { content: "▾ "; }
  details > summary:hover { color: #e6e6e6; }
  details.card-fold { margin: 0; border: 0; padding: 0; }
  details.card-fold > summary { font-size: 16px; color: #e6e6e6; font-weight: 500; }
  label { display: block; font-size: 13px; color: #aab2c0; margin: 10px 0 4px; }
  input[type=text], input[type=email], input[type=password], select {
    width: 100%; padding: 9px 11px; border-radius: 8px;
    border: 1px solid #303644; background: #0f1115; color: #e6e6e6; font-size: 14px;
  }
  code { background: #0b0d11; padding: 1px 5px; border-radius: 5px; font-size: 12px; }
  .row { display: flex; gap: 10px; align-items: center; }
  .row.checkbox { margin-top: 12px; font-size: 14px; color: #cbd2dd; }
  button {
    margin-top: 14px; padding: 9px 16px; border: 0; border-radius: 8px;
    background: #3b82f6; color: #fff; font-size: 14px; font-weight: 600; cursor: pointer;
  }
  button:hover { background: #2f6fe0; }
  button.secondary { background: #2a2f3a; color: #cbd2dd; }
  button.secondary:hover { background: #343a47; }
  button:disabled { opacity: .55; cursor: not-allowed; }
  button.preset { margin-top: 0; padding: 7px 12px; font-size: 13px; }
  button.preset.active { background: #3b82f6; color: #fff; }
  #library-list { font-size: 13px; }
  #drop-zone {
    border: 2px dashed #39404e; border-radius: 10px; padding: 26px 16px;
    text-align: center; cursor: pointer; transition: border-color .15s, background .15s;
  }
  #drop-zone:hover { border-color: #4b5563; }
  #drop-zone.dragging { border-color: #3b82f6; background: #1b2436; }
  #drop-zone strong { display: block; margin-bottom: 4px; color: #e6e6e6; }
  #import-status { margin-top: 10px; font-size: 13px; min-height: 18px; }
  .libitem {
    display: flex; justify-content: space-between; gap: 10px; align-items: center;
    padding: 8px 2px; border-bottom: 1px solid #20242d;
  }
  .libitem a { color: #7aa2ff; text-decoration: none; cursor: pointer; }
  .libitem a:hover { text-decoration: underline; }
  .muted { color: #8b93a1; font-size: 13px; }
  #log {
    background: #0b0d11; border: 1px solid #262b36; border-radius: 10px;
    padding: 12px; font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 12.5px; white-space: pre-wrap; word-break: break-word;
    max-height: 320px; overflow: auto; margin-top: 6px;
  }
  .log-ok { color: #5ad19a; }
  .log-err { color: #ff7b72; }
  .log-info { color: #79c0ff; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; }
  .pill.on { background: #143524; color: #5ad19a; }
  .pill.off { background: #3a1f23; color: #ff7b72; }
  .hidden { display: none !important; }
  a { color: #79c0ff; }
  a.back {
    display: inline-block; margin-bottom: 12px; text-decoration: none;
    color: #cbd2dd; background: #2a2f3a; padding: 6px 12px; border-radius: 8px;
    font-size: 13px; font-weight: 600;
  }
  a.back:hover { background: #343a47; }
</style>
</head>
<body>
<div class="wrap">
  <a id="back-link" href="/" class="back">&larr; Back to app</a>
  <a id="reader-link" href="/reader" target="_blank" class="back" style="margin-left:14px" title="Opens in its own window so this page keeps watching your export">📖 Open Reader</a>
  <h1>LNCrawl Tools</h1>
  <p class="sub">Lightweight power-user actions wired straight to the REST API.</p>

  <div class="card" id="auth-card">
    <h2>Sign in</h2>
    <p class="hint">Uses your existing account. The token is kept in this browser only.</p>
    <div id="logged-out">
      <label for="email">Email or username</label>
      <input id="email" type="text" autocomplete="username" />
      <label for="password">Password</label>
      <input id="password" type="password" autocomplete="current-password" />
      <button id="login-btn">Sign in</button>
    </div>
    <div id="logged-in" class="hidden">
      <p class="muted">Signed in as <span id="who" class="pill on"></span></p>
      <button id="logout-btn" class="secondary">Sign out</button>
    </div>
  </div>

  <div class="card">
    <h2>Discover all novels from a source</h2>
    <p class="hint">Paste a source website/base URL. Every novel the source exposes
      through its own search is discovered, de-duplicated, and queued for download.</p>
    <label for="src-url">Source URL</label>
    <input id="src-url" type="text" placeholder="https://novelsource.example/" />
    <div class="row checkbox">
      <input id="src-full" type="checkbox" />
      <label for="src-full" style="margin:0">Fetch full contents of every novel found</label>
    </div>
    <button id="discover-btn">Discover &amp; queue</button>
  </div>

  <div class="card">
    <h2>Download a whole source as a ZIP</h2>
    <p class="hint">Paste a source URL and download <b>every</b> novel from it, bundled into a
      single <code>.zip</code> you can save to a hard drive and read offline. Works for manga
      too — EPUB embeds the page images. Novels that fail or come out incomplete are
      automatically retried, so you can leave it running overnight. This runs in the
      background; when it's ready a download starts automatically.</p>
    <label for="exp-url">Source URL</label>
    <input id="exp-url" type="text" placeholder="https://novelsource.example/" />
    <div class="row">
      <div style="flex:2">
        <label for="exp-format">Format</label>
        <select id="exp-format">
          <option value="epub" selected>EPUB (best for manga &amp; most readers)</option>
          <option value="cbz">CBZ (manga only — comic reader archive)</option>
          <option value="pdf">PDF</option>
          <option value="azw3">AZW3 (Kindle)</option>
          <option value="mobi">MOBI (older Kindle)</option>
          <option value="txt">TXT (text only)</option>
        </select>
      </div>
      <div style="flex:1">
        <label for="exp-limit">Limit (optional)</label>
        <input id="exp-limit" type="text" inputmode="numeric" placeholder="blank = all" />
      </div>
    </div>
    <label style="margin-top:12px">Speed</label>
    <div class="row" id="exp-presets">
      <button type="button" class="preset secondary" data-preset="fast">⚡ Fast</button>
      <button type="button" class="preset secondary" data-preset="balanced">⚖️ Balanced</button>
      <button type="button" class="preset secondary" data-preset="gentle">🐢 Gentle</button>
      <span class="muted" id="preset-note" style="align-self:center">Balanced — good default</span>
    </div>
    <details>
      <summary>Advanced options — pacing, retries, limits</summary>
      <div class="row">
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
      <button id="export-btn">Download all &rarr; ZIP</button>
      <button id="export-test" class="secondary" style="margin-top:14px">🩺 Test source</button>
      <button id="export-stop" class="secondary hidden" style="margin-top:14px">&#9632; Stop</button>
      <button id="export-resume" class="secondary hidden" style="margin-top:14px">&#9654; Resume last</button>
    </div>
  </div>

  <div class="card">
    <h2>Import novels from a ZIP</h2>
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
  </div>

  <div class="card">
    <details class="card-fold" id="library-card">
    <summary>My exports (library)</summary>
    <p class="hint">Past source exports on this machine. Click to re-download the ZIP.</p>
    <div id="library-list"><span class="muted">Sign in to see your exports.</span></div>
    <button id="library-refresh" class="secondary">Refresh</button>
    </details>
  </div>

  <div class="card">
    <details class="card-fold">
    <summary>Storage, backup &amp; phone</summary>
    <p class="hint" id="storage-line">Sign in to see disk usage.</p>
    <div id="storage-largest" class="muted" style="font-size:13px"></div>
    <div class="row" style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap">
      <button id="storage-refresh" class="secondary">Check usage</button>
      <button id="storage-clean" class="secondary" title="Deletes generated export ZIPs (your novels stay). Backups are kept.">🧹 Clean export zips</button>
      <button id="backup-btn" class="secondary" title="Zips the database + every downloaded novel. Saved to the exports folder and your Desktop.">💾 Create backup</button>
      <button id="phone-btn" class="secondary" title="Show a QR code to open the Reader on your phone (same Wi-Fi)">📱 Read on phone</button>
    </div>
    <div id="phone-box" class="hidden" style="margin-top:12px;text-align:center">
      <div id="phone-qr" style="background:#fff;display:inline-block;padding:10px;border-radius:10px"></div>
      <p class="muted" id="phone-url" style="word-break:break-all"></p>
      <p class="hint">Scan with your phone's camera (same Wi-Fi as this PC), then sign in with the
        same email &amp; password. If it doesn't load, allow the app through Windows Firewall
        and make sure both devices are on the same network.</p>
    </div>
    </details>
  </div>

  <div class="card">
    <details class="card-fold">
    <summary>Finish notifications</summary>
    <p class="hint">Get pinged when a long overnight export finishes so you don't have to keep
      checking. A desktop notification pops on this machine; a webhook can reach your phone
      (Discord or Slack incoming webhook). Both are optional.</p>
    <div class="row checkbox">
      <input id="notify-desktop" type="checkbox" checked />
      <label for="notify-desktop" style="margin:0">Show a desktop notification when an export finishes</label>
    </div>
    <label for="notify-webhook">Webhook URL (optional)</label>
    <input id="notify-webhook" type="text" placeholder="https://discord.com/api/webhooks/..." />
    <button id="notify-save">Save notification settings</button>
    </details>
  </div>

  <div class="card">
    <details class="card-fold">
    <summary>Check completeness and repair EPUB</summary>
    <p class="hint">Check downloaded, missing, failed and empty chapters. Repair retries only
      chapter gaps and rebuilds the EPUB once all chapter text is available.</p>
    <label for="novel-id">Novel ID</label>
    <input id="novel-id" type="text" placeholder="e.g. 0c3b…" />
    <button id="completeness-btn">Check completeness</button>
    <button id="retry-btn">Retry missing and rebuild EPUB</button>
    </details>
  </div>

  <div class="card">
    <h2>Activity</h2>
    <div id="log"><span class="muted">Responses will appear here.</span></div>
  </div>

  <p class="sub" style="text-align:center;margin-top:8px">__BUILD_INFO__</p>
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

  function refreshAuthUI() {
    var inEl = document.getElementById("logged-in");
    var outEl = document.getElementById("logged-out");
    if (token()) {
      inEl.classList.remove("hidden");
      outEl.classList.add("hidden");
      api("/auth/me", { method: "GET" })
        .then(function (u) {
          document.getElementById("who").textContent = (u && (u.email || u.name)) || "user";
          window._lncrawlUserId = u && u.id;
          loadActiveExports(u && u.id);
          loadLibrary(u && u.id);
          loadNotifyConfig();
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

  function pollJob(id, label) {
    api("/job/" + id, { method: "GET" })
      .then(function (job) {
        var status = statusName(job.status);
        log(label + " · " + status + " (" + job.done + "/" + job.total + ")", "log-info");
        if (job.extra && job.extra.completeness) {
          var c = job.extra.completeness;
          log((c.complete ? "Complete" : "Incomplete") + " — " + c.downloaded + "/" + c.total +
            " downloaded, " + c.missing + " missing, " + c.failed + " failed, " + c.empty + " empty.", "log-info");
        }
        if (isActive(status)) {
          setTimeout(function () { pollJob(id, label); }, 3000);
        } else if (status === "FAILED" && job.error) {
          log(label + " failed: " + job.error, "log-err");
        }
      })
      .catch(function (e) { log("Status check failed: " + e.message, "log-err"); });
  }

  document.getElementById("discover-btn").addEventListener("click", function () {
    if (!requireAuth()) return;
    var btn = this;
    var url = document.getElementById("src-url").value.trim();
    var full = document.getElementById("src-full").checked;
    if (!url) { log("Enter a source URL.", "log-err"); return; }
    busy(btn, true);
    log("Queuing discovery for " + url + " …", "log-info");
    api("/job/create/discover-source", { method: "POST", body: JSON.stringify({ url: url, full: full }) })
      .then(function (job) {
        log("Discovery job created: " + job.id, "log-ok");
        pollJob(job.id, "Discover");
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
    if (ex.requests_per_sec) bits.push(ex.requests_per_sec + " req/s");
    if (ex.adaptive_delay) bits.push("+" + ex.adaptive_delay + "s/novel");
    return bits.length ? " [" + bits.join(", ") + "]" : "";
  }

  function pollExport(id, fails) {
    fails = fails || 0;
    api("/job/" + id, { method: "GET" })
      .then(function (job) {
        var status = statusName(job.status);
        var ex = job.extra || {};
        if (status === "RUNNING") setExportControls(id, id);
        if (status === "PENDING") {
          // queued behind another export — say so once, then wait quietly
          pollExport._queued = pollExport._queued || {};
          if (!pollExport._queued[id]) {
            pollExport._queued[id] = 1;
            log("⏳ Queued — will start automatically when the current export finishes.", "log-info");
          }
        } else if (status === "RUNNING" && ex.phase === "discovering") {
          log("Discovering novels… " + (ex.found || 0) + " found (" + job.done + "/" + job.total + " searches + sitemap pages)", "log-info");
        } else if (status === "RUNNING" && ex.phase === "solving-challenge") {
          log("Site is blocking requests — solving the Cloudflare challenge in a browser…", "log-info");
        } else if (status === "RUNNING" && ex.phase === "downloading" && ex.current_title) {
          var ch = ex.current_total_chapters ? " — ch " + (ex.current_chapters || 0) + "/" + ex.current_total_chapters : "";
          var resumed = ex.resumed ? " · " + ex.resumed + " reused from last run" : "";
          log("Downloading " + job.done + "/" + job.total + ": " + ex.current_title + ch + rateNote(ex) + resumed, "log-info");
          if (ex.last_fail_title && ex.last_fail_reason) {
            log("  ⚠ last issue: " + ex.last_fail_title + " — " + ex.last_fail_reason +
              (ex.last_fail_detail ? " (" + ex.last_fail_detail + ")" : ""), "log-err");
          }
        } else if (status === "RUNNING" && ex.phase === "retry-waiting") {
          log("Retry " + (ex.retry || "") + " — waiting for the source to recover…", "log-info");
        } else if (status === "RUNNING" && ex.phase === "checking-updates") {
          log("Checking finished novels for new chapters… (" + job.done + "/" + job.total + ")", "log-info");
        } else {
          log("Export · " + status + " (" + job.done + "/" + job.total + ")", "log-info");
        }
        if (isActive(status)) {
          setTimeout(function () { pollExport(id, 0); }, 4000);
        } else if (status === "SUCCESS") {
          setExportControls(null, id);
          var extra = job.extra || {};
          var parts = [(extra.complete != null ? extra.complete : extra.exported || 0) + " complete"];
          if (extra.incomplete) parts.push(extra.incomplete + " partial");
          if (extra.skipped) parts.push(extra.skipped + " skipped (already had)");
          if (extra.failed) parts.push(extra.failed + " failed");
          log("Export done: " + parts.join(", ") + " of " + (extra.total_novels || 0) + ".",
            extra.failed ? "log-info" : "log-ok");
          if (extra.discovery_capped) {
            log("  ⚠ Discovery stopped at its time cap, so this is only part of the source's " +
              "catalogue. Raise “Max discovery (min)” in Advanced options and run it again — " +
              "finished novels are reused.", "log-info");
          }
          (extra.fail_reasons || []).forEach(function (fr) {
            log("  " + fr.count + "× " + fr.reason, "log-err");
          });
          if (extra.saved_to) log("Saved to Desktop: " + extra.saved_to, "log-ok");
          if (extra.export_file) downloadBlob(id, extra.export_name);
          loadLibrary(window._lncrawlUserId);
        } else {
          setExportControls(null, id);
          log("Export ended: " + status + (job.error ? " — " + job.error : "") +
            ". Use the Resume last button to pick up where it left off.", "log-err");
        }
      })
      .catch(function (e) {
        // A failed poll cannot establish whether the server or export is still
        // running. Keep reconnecting without claiming the job is healthy.
        var n = fails + 1;
        if (n === 1) log("Status check hiccup — still retrying… (" + e.message + ")", "log-err");
        else if (n === 15) log("Still can't reach the server — export status is unknown; retrying every 15s. Check that the app is still running before starting another export.", "log-err");
        setTimeout(function () { pollExport(id, n); }, n < 15 ? 8000 : 15000);
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
    return body;
  }

  document.getElementById("export-btn").addEventListener("click", function () {
    if (!requireAuth()) return;
    var btn = this;
    var body = buildExportBody();
    if (!body) return;
    busy(btn, true);
    log("Starting full export of " + body.url + " (this can take a while) …", "log-info");
    api("/job/create/export-source", { method: "POST", body: JSON.stringify(body) })
      .then(function (job) {
        if (activeExportId && activeExportId !== job.id) {
          log("Export job created: " + job.id + " — queued behind the running export; it starts automatically when that one succeeds or fails.", "log-ok");
        } else {
          log("Export job created: " + job.id, "log-ok");
        }
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
    api("/job/" + id, { method: "GET" })
      .then(function (job) {
        var ex = job.extra || {};
        if (isActive(job.status)) {
          var seen = (ex.imported || 0) + (ex.updated || 0);
          importStatus.textContent = name + ": " +
            (ex.current ? "reading " + ex.current : "starting") +
            " — " + seen + " novel(s), " + (ex.chapters || 0) + " chapters so far";
          setTimeout(function () { pollImport(id, name, 0); }, 2000);
          return;
        }
        importStatus.textContent = "";
        if (job.status === 2) {
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
        setTimeout(function () { pollImport(id, name, n); }, n < 10 ? 4000 : 10000);
      });
  }

  function uploadArchives(list) {
    if (!requireAuth()) return;
    var files = Array.prototype.slice.call(list || []);
    if (!files.length) return;

    var form = new FormData();
    var names = [];
    files.forEach(function (f) {
      form.append("files", f, f.name);
      names.push(f.name);
    });

    log("Uploading " + files.length + " file(s)…", "log-info");
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/import");
    xhr.setRequestHeader("Authorization", "Bearer " + token());
    // Let the browser set the multipart content type: it has to carry the
    // boundary, and naming it here without one makes the body unparseable.
    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      importStatus.textContent = "Uploading… " + Math.round((e.loaded / e.total) * 100) + "%";
    };
    xhr.onload = function () {
      importStatus.textContent = "";
      var res = null;
      try { res = JSON.parse(xhr.responseText || "null"); } catch (err) { res = null; }
      if (xhr.status < 200 || xhr.status >= 300) {
        var msg = (res && (res.error || res.detail || res.message)) || ("HTTP " + xhr.status);
        log("Upload failed: " + msg, "log-err");
        return;
      }
      (res || []).forEach(function (job, i) {
        var name = names[i] || "archive";
        log("Importing " + name + "…", "log-info");
        pollImport(job.id, name, 0);
      });
    };
    xhr.onerror = function () {
      importStatus.textContent = "";
      log("Upload failed — the connection dropped.", "log-err");
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
  document.getElementById("library-refresh").addEventListener("click", function () {
    if (!requireAuth()) return;
    loadLibrary(window._lncrawlUserId);
  });

  // ---- Storage, backup & phone ----
  function loadStorage() {
    api("/novels/storage", { method: "GET" }).then(function (s) {
      document.getElementById("storage-line").textContent =
        (s.novel_count || 0) + " novel(s) use " + (fmtSize(s.total || 0) || "0 MB") +
        " · export zips use " + (fmtSize(s.exports || 0) || "0 MB");
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
  document.getElementById("storage-refresh").addEventListener("click", function () {
    if (!requireAuth()) return;
    loadStorage();
  });
  document.getElementById("storage-clean").addEventListener("click", function () {
    if (!requireAuth()) return;
    if (!confirm("Delete all generated export ZIPs? Your downloaded novels are not touched; backups are kept.")) return;
    api("/novels/storage/exports", { method: "DELETE" }).then(function (r) {
      log("Removed " + r.removed + " zip(s), freed " + (fmtSize(r.freed) || "0 MB") + ".", "log-ok");
      loadStorage();
    }).catch(function (e) { log("Cleanup failed: " + e.message, "log-err"); });
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
})();
</script>
</body>
</html>
"""


@router.get("/tools", include_in_schema=False)
async def tools_page() -> HTMLResponse:
    from ..context import ctx

    info = f"Lightnovel Crawler v{ctx.config.app.version} · build {ctx.config.app.build}"
    return HTMLResponse(content=_TOOLS_HTML.replace("__BUILD_INFO__", info))
