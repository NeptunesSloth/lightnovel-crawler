"""The Downloads page: every download in one place.

Shows each download still going (running, resting, queued or paused) with its
progress, what it is working on, how fast and how long is left, and why it is
waiting when it waits; then the ones that finished recently. Site downloads can
be paused, resumed and stopped from here, and finished or stopped ones retried.

Self-contained like the Tools page (``/tools``), so it survives front-end
rebuilds and works from the desktop app. Served at ``GET /downloads``.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

# A raw string: the page's script is kept exactly as written.
_DOWNLOADS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>LNCrawl · Downloads</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #0f1115; color: #e6e6e6; line-height: 1.5;
  }
  .wrap { max-width: 820px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 2px; }
  .sub { color: #8b93a1; margin: 0 0 16px; font-size: 14px; }
  a.back {
    display: inline-block; margin: 0 10px 12px 0; text-decoration: none;
    color: #cbd2dd; background: #2a2f3a; padding: 6px 12px; border-radius: 8px;
    font-size: 13px; font-weight: 600;
  }
  a.back:hover { background: #343a47; }
  .bar-top { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
  .bar-top label { font-size: 13px; color: #aab2c0; display: flex; gap: 6px; align-items: center; }
  .card {
    background: #171a21; border: 1px solid #262b36; border-radius: 12px;
    padding: 14px 16px; margin-bottom: 12px;
  }
  .card.done { opacity: .82; }
  .head { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
  .title { font-size: 15px; font-weight: 600; word-break: break-word; }
  .chip {
    flex: none; font-size: 12px; padding: 2px 9px; border-radius: 999px;
    background: #22324d; color: #9cc0ff; white-space: nowrap;
  }
  .chip.paused, .chip.queued { background: #2a2f3a; color: #cbd2dd; }
  .chip.wait { background: #3a2f12; color: #f0c46a; }
  .chip.ok { background: #143524; color: #5ad19a; }
  .chip.bad { background: #3a1f23; color: #ff7b72; }
  .progress { height: 6px; background: #262b36; border-radius: 4px; margin: 10px 0 6px; overflow: hidden; }
  .progress span { display: block; height: 100%; background: #3b82f6; width: 0; transition: width .4s; }
  .line { font-size: 13px; color: #aab2c0; margin-top: 3px; word-break: break-word; }
  .line.warn { color: #f0c46a; }
  .line.err { color: #ff7b72; }
  .btns { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
  button {
    padding: 7px 13px; border: 0; border-radius: 8px; font-size: 13px; font-weight: 600;
    background: #2a2f3a; color: #cbd2dd; cursor: pointer;
  }
  button:hover { background: #343a47; }
  button.primary { background: #3b82f6; color: #fff; }
  button.primary:hover { background: #2f6fe0; }
  button:disabled { opacity: .55; cursor: not-allowed; }
  .empty { color: #8b93a1; text-align: center; padding: 30px 10px; }
  h2 { font-size: 14px; color: #8b93a1; font-weight: 600; margin: 20px 0 8px; text-transform: uppercase; letter-spacing: .04em; }
  .hidden { display: none !important; }
  #signin input { width: 100%; padding: 9px 11px; border-radius: 8px; border: 1px solid #303644; background: #0f1115; color: #e6e6e6; margin: 4px 0 8px; }
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/">&larr; App</a>
  <a class="back" href="/reader" id="reader-link">📖 Reader</a>
  <a class="back" href="/tools" id="tools-link">🛠 Tools</a>
  <h1>Downloads</h1>
  <p class="sub" id="summary">Loading…</p>

  <div class="card hidden" id="signin">
    <div class="title">Sign in</div>
    <input id="email" type="text" placeholder="Email" autocomplete="username" />
    <input id="password" type="password" placeholder="Password" autocomplete="current-password" />
    <button class="primary" id="login-btn">Sign in</button>
    <div class="line err" id="login-err"></div>
  </div>

  <div class="bar-top" id="controls">
    <label><input type="checkbox" id="show-done" checked /> Show finished</label>
    <label><input type="checkbox" id="notify" /> Notify me when one ends</label>
  </div>
  <div id="active"></div>
  <h2 id="done-head" class="hidden">Finished</h2>
  <div id="done"></div>
</div>
<script>
(function () {
  var KEY = "lncrawl_token";
  try {
    var params = new URLSearchParams(location.search);
    var t = params.get("authToken");
    if (t) {
      localStorage.setItem(KEY, t);
      params.delete("authToken");
      history.replaceState(null, "", location.pathname + (params.toString() ? "?" + params : ""));
    }
  } catch (e) {}
  function token() { try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; } }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (token()) opts.headers.Authorization = "Bearer " + token();
    return fetch("/api" + path, opts).then(function (res) {
      if (res.status === 401) { showSignin(); throw new Error("Please sign in"); }
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
        if (!res.ok) throw new Error((data && (data.detail || data.message)) || ("HTTP " + res.status));
        return data;
      });
    });
  }

  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function showSignin() { $("signin").classList.remove("hidden"); $("controls").classList.add("hidden"); }
  $("login-btn").addEventListener("click", function () {
    api("/auth/login", { method: "POST", body: JSON.stringify({ email: $("email").value, password: $("password").value }) })
      .then(function (res) {
        localStorage.setItem(KEY, res.token);
        $("signin").classList.add("hidden"); $("controls").classList.remove("hidden");
        refresh();
      })
      .catch(function (e) { $("login-err").textContent = e.message; });
  });

  // ---- wording ----
  var SITE_DOWNLOAD = 53;
  function clock(ms) {
    var d = new Date(ms);
    var s = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return d.toDateString() === new Date().toDateString() ? s : d.toLocaleDateString([], { weekday: "short" }) + " " + s;
  }
  function span(seconds) {
    if (seconds == null || seconds < 0) return "";
    var m = Math.round(seconds / 60);
    if (m < 1) return "under a minute";
    if (m < 60) return m + " min";
    var h = Math.floor(m / 60), d = Math.floor(h / 24);
    if (d >= 1) return d + "d " + (h % 24) + "h";
    return h + "h " + (m % 60) + "m";
  }
  function ago(ms) { return ms ? span((Date.now() - ms) / 1000) + " ago" : ""; }

  // [label, chip style, explanation]
  function state(j) {
    var ex = j.extra || {};
    if (j.status === "PAUSED") return ["Paused", "paused", "Resume to carry on where it stopped."];
    if (j.status === "PENDING") return ["Queued", "queued", "Starts when a slot is free — one download per site, a few sites at once."];
    if (j.status === "SUCCESS") return ["Done", "ok", ""];
    if (j.status === "FAILED") return ["Failed", "bad", ""];
    if (j.status === "CANCELED") return ["Stopped", "bad", ""];
    var until = ex.resume_at ? " — carries on at " + clock(ex.resume_at) : "";
    switch (ex.phase) {
      case "discovering": return ["Finding novels", "", (ex.found || 0) + " found so far"];
      case "fetching": return ["Adding novel details", "", ""];
      case "checking-updates": return ["Checking for new chapters", "", ""];
      case "solving-challenge": return ["Getting past a bot check", "wait", "The site asked for a check; solving it in a browser."];
      case "waiting-between-novels": return ["Short pause", "wait", "Easing off after an error (" + (ex.adaptive_delay || 0) + "s)."];
      case "retry-waiting":
        return ["Resting the site", "wait", (ex.quiet_rounds ? "The last round got nothing new, so it is giving the site a rest" : "Waiting briefly before the next round") + until + "."];
      case "cooling-down":
        var why = { stalled: "Downloads stopped responding, so it started fresh ones",
          "blocked-discovery": "The site isn't answering searches (probably blocking for now)",
          restarting: "Something unexpected went wrong; restarting from where it was" }[ex.cool_reason] || "Giving the site a rest";
        return ["Waiting it out", "wait", why + until + "."];
      case "disk-full": return ["Disk full", "bad", "Free up some space — it carries on by itself" + until + "."];
      case "downloading-images": return ["Downloading pages", "", ""];
      default: return ["Downloading", "", ""];
    }
  }

  function novelsDone(j) {
    var ex = j.extra || {};
    if (j.type === SITE_DOWNLOAD && ex.total_novels) {
      var done = ex.done_novels != null ? ex.done_novels : j.done;
      return [Math.min(done, ex.total_novels), ex.total_novels, "novels"];
    }
    return [j.done || 0, j.total || 0, "items"];
  }

  // ---- actions ----
  function act(btn, path, after) {
    btn.disabled = true;
    api(path, { method: "POST" }).then(function (r) { if (after) after(r); refresh(); })
      .catch(function (e) { alert(e.message); btn.disabled = false; });
  }
  function downloadZip(j) {
    fetch("/api/job/" + j.id + "/export", { headers: { Authorization: "Bearer " + token() } })
      .then(function (res) { if (!res.ok) throw new Error("HTTP " + res.status); return res.blob(); })
      .then(function (blob) {
        var a = document.createElement("a"), url = URL.createObjectURL(blob);
        a.href = url; a.download = (j.extra || {}).export_name || "novels.zip";
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
      })
      .catch(function (e) { alert("Couldn't download: " + e.message); });
  }

  // ---- drawing ----
  function card(j) {
    var ex = j.extra || {};
    var st = state(j);
    var c = el("div", "card" + (j.is_done ? " done" : ""));
    c.dataset.id = j.id;
    var head = el("div", "head");
    head.appendChild(el("div", "title", j.title || "Download"));
    head.appendChild(el("span", "chip " + st[1], st[0]));
    c.appendChild(head);

    var nd = novelsDone(j);
    if (nd[1] > 1 || (!j.is_done && nd[1])) {
      var bar = el("div", "progress"), fill = el("span");
      fill.style.width = (nd[1] ? Math.min(100, Math.round(100 * nd[0] / nd[1])) : 0) + "%";
      bar.appendChild(fill); c.appendChild(bar);
      c.appendChild(el("div", "line", nd[0] + " of " + nd[1] + " " + nd[2] +
        (ex.resumed ? " · " + ex.resumed + " reused from before" : "")));
    }
    if (st[2]) c.appendChild(el("div", "line" + (st[1] === "bad" ? " err" : st[1] === "wait" ? " warn" : ""), st[2]));

    if (!j.is_done && j.status === "RUNNING") {
      var now = (ex.active_novels && ex.active_novels.length) ? ex.active_novels
        : (ex.current_title ? [{ title: ex.current_title, done: ex.current_chapters || 0, total: ex.current_total_chapters || 0 }] : []);
      now = now.slice().sort(function (a, b) { return String(a.title).localeCompare(String(b.title)); });
      now.forEach(function (a) {
        c.appendChild(el("div", "line", "▸ " + a.title + (a.total ? " — chapter " + a.done + " of " + a.total : "")));
      });
      if (ex.phase === "downloading-images" && ex.current_total_images) {
        c.appendChild(el("div", "line", "▸ page " + (ex.current_images || 0) + " of " + ex.current_total_images));
      }
      var pace = [];
      if (ex.requests_per_sec) pace.push(ex.requests_per_sec + " requests/s");
      if (ex.chapters_per_min) pace.push(ex.chapters_per_min + " chapters/min");
      if (ex.eta_seconds != null && ex.eta_seconds >= 0) pace.push("about " + span(ex.eta_seconds) + " left at this pace");
      if (pace.length) c.appendChild(el("div", "line", pace.join(" · ")));
      var cuts = ex.slowdowns || {}, why = [];
      if (cuts.refused) why.push(cuts.refused + "× because the site refused requests");
      if (cuts.slow) why.push(cuts.slow + "× because many requests timed out");
      if (why.length) c.appendChild(el("div", "line warn", "Slowed down " + why.join(", ") + " in the last 10 minutes."));
      if (ex.last_fail_title && ex.last_fail_reason) {
        c.appendChild(el("div", "line warn", "Last issue: " + ex.last_fail_title + " — " + ex.last_fail_reason));
      }
    }

    if (j.is_done) {
      if (j.status === "SUCCESS" && j.type === SITE_DOWNLOAD) {
        var parts = [];
        if (ex.complete != null) parts.push(ex.complete + " complete");
        if (ex.incomplete) parts.push(ex.incomplete + " partial");
        if (ex.failed) parts.push(ex.failed + " failed");
        if (ex.skipped) parts.push(ex.skipped + " already had");
        if (parts.length) c.appendChild(el("div", "line", parts.join(" · ")));
        (ex.fail_reasons || []).slice(0, 3).forEach(function (r) {
          c.appendChild(el("div", "line warn", r.count + "× " + r.reason));
        });
      }
      if (j.error && j.status !== "SUCCESS") {
        var lines = String(j.error).trim().split("\n");
        c.appendChild(el("div", "line err", lines[lines.length - 1]));
      }
      var took = j.started_at && j.finished_at ? " · took " + span((j.finished_at - j.started_at) / 1000) : "";
      c.appendChild(el("div", "line", "Ended " + ago(j.finished_at || j.updated_at) + took));
    } else {
      c.appendChild(el("div", "line", "Started " + ago(j.started_at || j.created_at) +
        " · last activity " + ago(j.updated_at)));
    }

    var btns = el("div", "btns");
    var site = j.type === SITE_DOWNLOAD;
    if (!j.is_done) {
      if (site && j.status === "PAUSED") {
        var r = el("button", "primary", "▶ Resume"); r.addEventListener("click", function () { act(r, "/job/" + j.id + "/resume"); }); btns.appendChild(r);
      } else if (site) {
        var p = el("button", "", "⏸ Pause"); p.addEventListener("click", function () { act(p, "/job/" + j.id + "/pause"); }); btns.appendChild(p);
      }
      var s = el("button", "", "■ Stop");
      s.addEventListener("click", function () {
        if (!confirm("Stop this download? What it already downloaded is kept" + (site ? ", and Retry later continues from there." : "."))) return;
        act(s, "/job/" + j.id + "/cancel");
      });
      btns.appendChild(s);
    } else {
      if (j.status !== "SUCCESS" || (site && (ex.incomplete || ex.failed))) {
        var again = el("button", "primary", site ? "↻ Retry — continue where it stopped" : "↻ Retry");
        again.addEventListener("click", function () { act(again, "/job/" + j.id + "/replay"); });
        btns.appendChild(again);
      }
      if (ex.export_file) {
        var z = el("button", "", "⤓ Download ZIP"); z.addEventListener("click", function () { downloadZip(j); }); btns.appendChild(z);
      }
      if (site && ex.library_only) {
        var o = el("button", "", "📖 Read"); o.addEventListener("click", function () { location.href = "/reader"; }); btns.appendChild(o);
      }
    }
    if (btns.children.length) c.appendChild(btns);
    return c;
  }

  var lastStatus = {};
  function notifyEnds(rows) {
    rows.forEach(function (j) {
      var before = lastStatus[j.id];
      lastStatus[j.id] = j.status;
      if (!before || before === j.status || !j.is_done) return;
      if (!$("notify").checked || !window.Notification || Notification.permission !== "granted") return;
      try { new Notification((state(j)[0]) + ": " + (j.title || "Download")); } catch (e) {}
    });
  }
  $("notify").addEventListener("change", function () {
    try { localStorage.setItem("lncrawl_dl_notify", this.checked ? "1" : ""); } catch (e) {}
    if (this.checked && window.Notification && Notification.permission === "default") Notification.requestPermission();
  });
  $("show-done").addEventListener("change", function () {
    try { localStorage.setItem("lncrawl_dl_hide_done", this.checked ? "" : "1"); } catch (e) {}
    draw(lastRows);
  });
  try {
    $("notify").checked = !!localStorage.getItem("lncrawl_dl_notify");
    $("show-done").checked = !localStorage.getItem("lncrawl_dl_hide_done");
  } catch (e) {}

  var lastRows = [];
  function draw(rows) {
    lastRows = rows;
    var active = rows.filter(function (j) { return !j.is_done; });
    var done = rows.filter(function (j) { return j.is_done; });
    var counts = {};
    active.forEach(function (j) { var k = state(j)[0]; counts[k] = (counts[k] || 0) + 1; });
    var bits = Object.keys(counts).map(function (k) { return counts[k] + " " + k.toLowerCase(); });
    $("summary").textContent = active.length ? bits.join(" · ") : "Nothing downloading right now.";

    $("active").innerHTML = "";
    if (!active.length) $("active").appendChild(el("div", "card empty", "No downloads running. Start one from Tools (Download a whole site) or the Reader (Get chapters)."));
    active.forEach(function (j) { $("active").appendChild(card(j)); });

    var show = $("show-done").checked && done.length;
    $("done-head").classList.toggle("hidden", !show);
    $("done").innerHTML = "";
    if (show) done.forEach(function (j) { $("done").appendChild(card(j)); });
  }

  var timer = null;
  function refresh() {
    clearTimeout(timer);
    api("/jobs/downloads").then(function (rows) {
      rows = rows || [];
      notifyEnds(rows);
      draw(rows);
    }).catch(function (e) {
      $("summary").textContent = "Couldn't load downloads: " + e.message + " — retrying…";
    }).finally(function () {
      timer = setTimeout(refresh, document.hidden ? 20000 : 4000);
    });
  }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });
  if (token()) {
    $("reader-link").href = "/reader";
    $("tools-link").href = "/tools?authToken=" + encodeURIComponent(token());
  }
  if (!token()) showSignin();
  refresh();
})();
</script>
</body>
</html>
"""


@router.get("/downloads", include_in_schema=False)
async def downloads_page() -> HTMLResponse:
    return HTMLResponse(content=_DOWNLOADS_HTML)
