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
</style>
</head>
<body>
<div class="wrap">
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
      too — EPUB embeds the page images. This runs in the background; when it's ready a
      download starts automatically.</p>
    <label for="exp-url">Source URL</label>
    <input id="exp-url" type="text" placeholder="https://novelsource.example/" />
    <div class="row">
      <div style="flex:1">
        <label for="exp-format">Format</label>
        <select id="exp-format">
          <option value="epub" selected>EPUB (best for manga &amp; most readers)</option>
          <option value="pdf">PDF</option>
          <option value="azw3">AZW3 (Kindle)</option>
          <option value="mobi">MOBI (older Kindle)</option>
          <option value="txt">TXT (text only)</option>
        </select>
      </div>
      <div style="flex:1">
        <label for="exp-limit">Limit (optional)</label>
        <input id="exp-limit" type="text" inputmode="numeric" placeholder="e.g. 50 — blank = all" />
      </div>
    </div>
    <button id="export-btn">Download all &rarr; ZIP</button>
  </div>

  <div class="card">
    <h2>Retry missing / failed chapters</h2>
    <p class="hint">Re-fetches only the chapters that aren't downloaded yet for a novel —
      successfully downloaded chapters are skipped.</p>
    <label for="novel-id">Novel ID</label>
    <input id="novel-id" type="text" placeholder="e.g. 0c3b…" />
    <button id="retry-btn">Retry missing</button>
  </div>

  <div class="card">
    <h2>Activity</h2>
    <div id="log"><span class="muted">Responses will appear here.</span></div>
  </div>
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
          var detail = (data && (data.message || data.detail)) || text || res.statusText;
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

  function pollJob(id, label) {
    api("/job/" + id, { method: "GET" })
      .then(function (job) {
        var status = job.status;
        var done = job.done, total = job.total;
        log(label + " · " + status + " (" + done + "/" + total + ")", "log-info");
        if (status === "RUNNING" || status === "PENDING") {
          setTimeout(function () { pollJob(id, label); }, 3000);
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

  function pollExport(id) {
    api("/job/" + id, { method: "GET" })
      .then(function (job) {
        var status = job.status;
        log("Export · " + status + " (" + job.done + "/" + job.total + ")", "log-info");
        if (status === "RUNNING" || status === "PENDING") {
          setTimeout(function () { pollExport(id); }, 4000);
        } else if (status === "SUCCESS") {
          var extra = job.extra || {};
          log("Exported " + (extra.exported || 0) + " novels" +
            (extra.failed ? " (" + extra.failed + " failed)" : "") + ".", "log-ok");
          downloadBlob(id, extra.export_name);
        } else {
          log("Export ended: " + status + (job.error ? " — " + job.error : ""), "log-err");
        }
      })
      .catch(function (e) { log("Status check failed: " + e.message, "log-err"); });
  }

  document.getElementById("export-btn").addEventListener("click", function () {
    if (!requireAuth()) return;
    var btn = this;
    var url = document.getElementById("exp-url").value.trim();
    var format = document.getElementById("exp-format").value;
    var limitRaw = document.getElementById("exp-limit").value.trim();
    if (!url) { log("Enter a source URL.", "log-err"); return; }
    var body = { url: url, format: format };
    if (limitRaw) {
      var limit = parseInt(limitRaw, 10);
      if (!isNaN(limit) && limit > 0) body.limit = limit;
    }
    busy(btn, true);
    log("Starting full export of " + url + " (this can take a while) …", "log-info");
    api("/job/create/export-source", { method: "POST", body: JSON.stringify(body) })
      .then(function (job) {
        log("Export job created: " + job.id, "log-ok");
        pollExport(job.id);
      })
      .catch(function (e) { log("Export failed: " + e.message, "log-err"); })
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
    return HTMLResponse(content=_TOOLS_HTML)
