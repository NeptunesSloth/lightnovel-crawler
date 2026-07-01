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
        <input id="exp-autotune" type="checkbox" checked />
        <label for="exp-autotune" style="margin:0">Auto-tune rate — slow down when blocked, speed up when clear</label>
      </div>
      <p class="hint" style="margin-top:10px">Defaults work for most sites. If a site blocks the run,
        it auto-slows and (when a browser is available) solves the Cloudflare challenge by itself; turn
        on Polite mode or lower Requests/sec to 0.5/0.25 for stubborn ones.</p>
    </details>
    <div class="row" style="margin-top:14px">
      <button id="export-btn">Download all &rarr; ZIP</button>
      <button id="export-test" class="secondary" style="margin-top:14px">🩺 Test source</button>
      <button id="export-stop" class="secondary hidden" style="margin-top:14px">&#9632; Stop</button>
      <button id="export-resume" class="secondary hidden" style="margin-top:14px">&#9654; Resume last</button>
    </div>
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
    <summary>Retry missing / failed chapters</summary>
    <p class="hint">Re-fetches only the chapters that aren't downloaded yet for a novel —
      successfully downloaded chapters are skipped.</p>
    <label for="novel-id">Novel ID</label>
    <input id="novel-id" type="text" placeholder="e.g. 0c3b…" />
    <button id="retry-btn">Retry missing</button>
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

  function log(msg, cls) {
    if (firstLog) { logEl.innerHTML = ""; firstLog = false; }
    var line = document.createElement("div");
    if (cls) line.className = cls;
    var ts = new Date().toLocaleTimeString();
    line.textContent = "[" + ts + "] " + msg;
    logEl.appendChild(line);
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
        if (isActive(status)) setExportControls(id, id);
        if (status === "RUNNING" && ex.phase === "discovering") {
          log("Discovering novels… " + (ex.found || 0) + " found (" + job.done + "/" + job.total + " searches)", "log-info");
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
        // a transient blip (busy server / connection hiccup) shouldn't freeze the
        // progress view — the export keeps running server-side, so NEVER give up:
        // keep retrying forever, just more slowly after repeated failures
        var n = fails + 1;
        if (n === 1) log("Status check hiccup — still retrying… (" + e.message + ")", "log-err");
        else if (n === 15) log("Still can't reach the server — the export keeps running; retrying every 15s…", "log-err");
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
        log("Export job created: " + job.id, "log-ok");
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
