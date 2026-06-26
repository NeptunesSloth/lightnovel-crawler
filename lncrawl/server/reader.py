"""Standalone in-app Reader page.

A self-contained, dependency-free reading experience served at ``GET /reader``.
It browses the downloaded library, lists a novel's chapters, and renders chapter
content (text and manga images) with next/previous navigation, adjustable font
size, and per-novel reading-position memory. Kept outside ``lncrawl/server/web``
(overwritten by the web-sync workflow) so it survives front-end rebuilds; the
markup is embedded as a string so it always ships with the wheel/exe.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

# NOTE: plain string (not an f-string) on purpose — the embedded CSS/JS is full
# of ``{}`` braces. Avoid backslash escapes inside the JS strings.
_READER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
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
</style>
</head>
<body>
<div class="topbar">
  <h1>📖 LNCrawl Reader</h1>
  <a class="back hidden" id="nav-back">&larr; Back</a>
  <div class="spacer"></div>
  <div id="search-wrap" style="flex:1;max-width:380px"><input id="search" type="text" placeholder="Search your library…" /></div>
  <a class="back" href="/tools" id="tools-link">Tools</a>
  <button id="font-dn" class="hidden">A-</button>
  <button id="font-up" class="hidden">A+</button>
</div>

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
    <p class="muted" id="lib-status">Loading your library…</p>
    <div id="lib-list"></div>
  </div>

  <div id="view-novel" class="hidden">
    <h2 id="novel-title"></h2>
    <p class="meta muted" id="novel-meta"></p>
    <button id="continue-btn" class="primary hidden" style="margin-bottom:12px">▶ Continue reading</button>
    <button id="heal-btn" class="hidden" style="margin:0 0 12px 8px" title="Fill gaps from another copy of this novel in your library">✨ Fill missing chapters</button>
    <span class="muted hidden" id="heal-msg" style="margin-left:8px"></span>
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
      <span class="muted" id="reader-pos"></span>
      <div class="spacer" style="flex:1"></div>
      <button id="next-btn" class="primary">Next &rarr;</button>
    </div>
  </div>
</div>

<script>
(function () {
  var KEY = "lncrawl_token";
  var POS = "lncrawl_reader_pos";    // { novelId: chapterId }
  var FONT = "lncrawl_reader_font";

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
  function savePos(novelId, chapterId) {
    var p = loadPos(); p[novelId] = chapterId; localStorage.setItem(POS, JSON.stringify(p));
  }

  var els = {};
  ["nav-back","search","view-library","lib-status","lib-list","view-novel","novel-title",
   "novel-meta","continue-btn","heal-btn","heal-msg","chap-list","chap-more-wrap","chap-more",
   "view-reader","reader-title","reader-content","prev-btn","next-btn","reader-pos","font-up",
   "font-dn","auth","email","password","login","auth-msg","search-wrap"].forEach(function (id) {
    els[id] = document.getElementById(id);
  });

  function show(view) {
    ["view-library","view-novel","view-reader","auth"].forEach(function (v) {
      els[v].classList.toggle("hidden", v !== view);
    });
    els["nav-back"].classList.toggle("hidden", view === "view-library" || view === "auth");
    els["search-wrap"].classList.toggle("hidden", view !== "view-library");
    var inReader = view === "view-reader";
    els["font-up"].classList.toggle("hidden", !inReader);
    els["font-dn"].classList.toggle("hidden", !inReader);
  }

  // ---- Library ----
  function loadLibrary(q) {
    els["lib-status"].textContent = "Loading your library…";
    els["lib-list"].innerHTML = "";
    api("/novels?limit=100&search=" + encodeURIComponent(q || "")).then(function (res) {
      var items = (res && res.items) || [];
      if (!items.length) {
        els["lib-status"].textContent = q ? "No matches." : "Your library is empty — download something first (Tools).";
        return;
      }
      els["lib-status"].textContent = items.length + " novel(s)";
      items.forEach(function (n) {
        var c = document.createElement("div");
        c.className = "card";
        var h = document.createElement("h3"); h.textContent = n.title || "Untitled";
        var m = document.createElement("div"); m.className = "meta";
        m.textContent = [n.authors || "", (n.chapter_count || 0) + " chapters", n.domain || ""]
          .filter(Boolean).join("  ·  ");
        c.appendChild(h); c.appendChild(m);
        c.addEventListener("click", function () { openNovel(n); });
        els["lib-list"].appendChild(c);
      });
    }).catch(function (e) { els["lib-status"].textContent = "Couldn't load library: " + e.message; });
  }

  // ---- Novel ----
  var current = { novel: null, chapters: [], offset: 0 };
  function openNovel(novel) {
    current = { novel: novel, chapters: [], offset: 0 };
    els["novel-title"].textContent = novel.title || "Untitled";
    els["novel-meta"].textContent = [novel.authors || "", (novel.chapter_count || 0) + " chapters", novel.domain || ""]
      .filter(Boolean).join("  ·  ");
    els["chap-list"].innerHTML = "";
    var pos = loadPos()[novel.id];
    els["continue-btn"].classList.toggle("hidden", !pos);
    els["continue-btn"].onclick = function () { if (pos) openChapter(pos); };
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
      if (gaps) els["heal-btn"].classList.remove("hidden");
    }).catch(function (e) { els["chap-list"].innerHTML = "<p class='muted'>Couldn't load chapters: " + e.message + "</p>"; });
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

  // ---- Reader ----
  function openChapter(chapterId) {
    show("view-reader");
    els["reader-title"].textContent = "Loading…";
    els["reader-content"].innerHTML = "";
    els["reader-pos"].textContent = "";
    window.scrollTo(0, 0);
    api("/chapter/" + chapterId + "/read?auto_fetch=false").then(function (res) {
      var ch = res.chapter || {};
      var novel = res.novel || current.novel || {};
      current.novel = novel;
      els["reader-title"].textContent = ch.title || ("Chapter " + ch.serial);
      els["reader-content"].innerHTML = res.content || "<p class='muted'>No content available for this chapter.</p>";
      els["reader-pos"].textContent = "#" + (ch.serial || "");
      hydrateImages(els["reader-content"]);
      savePos(novel.id, chapterId);
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
  // the authenticated image endpoint and swap in an object URL.
  function hydrateImages(root) {
    var imgs = root.querySelectorAll("img[src]");
    imgs.forEach(function (img) {
      var src = img.getAttribute("src") || "";
      var m = src.match(/images\\/([0-9a-fA-F]+)\\.jpg/);
      if (!m) return;
      img.removeAttribute("src");
      fetch("/api/chapter/image/" + m[1], { headers: { "Authorization": "Bearer " + token() } })
        .then(function (r) { if (!r.ok) throw new Error("img"); return r.blob(); })
        .then(function (b) { img.src = URL.createObjectURL(b); })
        .catch(function () { img.alt = "(image unavailable)"; });
    });
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

  // ---- Auth ----
  els["login"].addEventListener("click", function () {
    var email = els["email"].value.trim(), password = els["password"].value;
    if (!email || !password) { els["auth-msg"].textContent = "Enter email and password."; return; }
    api("/auth/login", { method: "POST", body: JSON.stringify({ email: email, password: password }) })
      .then(function (res) { localStorage.setItem(KEY, res.token); start(); })
      .catch(function (e) { els["auth-msg"].textContent = "Sign in failed: " + e.message; });
  });

  var toolsLink = els["tools-link"];
  function start() {
    if (toolsLink) toolsLink.href = "/tools" + (token() ? ("?authToken=" + encodeURIComponent(token())) : "");
    applyFont();
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


@router.get("/reader", include_in_schema=False)
async def reader_page() -> HTMLResponse:
    return HTMLResponse(content=_READER_HTML)
