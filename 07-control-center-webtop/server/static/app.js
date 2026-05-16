// Minimal URL-bridge for the webtop MVP.
//
// 1. Set the iframe src to https://<this-hostname>:VIEWER_PORT/.
// 2. Watch our own URL; on change, debounce and POST params to /api/params.
// 3. `?url=...` is treated as a shortcut for `open_url=...`.
//
// The control-center serves HTTP; the iframe target is HTTPS (LSIO webtop's
// built-in self-signed cert). The user accepts that cert once via a direct
// visit to the iframe URL (see README), then the iframe loads.

const VIEWER_PORT = 5088;
const POLL_MS = 1000;
const DEBOUNCE_MS = 500;
const RESERVED = new Set(["url"]);

const $ = (id) => document.getElementById(id);

const viewerUrl = `https://${location.hostname}:${VIEWER_PORT}/`;
$("viewer").src = viewerUrl;

function readParams() {
  const sp = new URLSearchParams(location.search);
  const params = {};
  for (const [k, v] of sp.entries()) {
    if (RESERVED.has(k)) continue;
    params[k] = v;
  }
  const u = sp.get("url");
  if (u && !params.open_url) params.open_url = u;
  return params;
}

let lastSent = null;
let timer = null;

function setStatus(s, isErr = false) {
  const el = $("status");
  el.textContent = s;
  el.className = isErr ? "err" : "";
}

async function push() {
  const params = readParams();
  const sig = JSON.stringify(params);
  if (sig === lastSent) return;
  lastSent = sig;
  setStatus("syncing…");
  try {
    const r = await fetch("/api/params", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ params }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    setStatus(`synced: ${sig === "{}" ? "(empty)" : sig}`);
  } catch (e) {
    setStatus(`error: ${e.message}`, true);
    lastSent = null;  // retry on next tick
  }
}

function scheduleSync() {
  if (timer) clearTimeout(timer);
  timer = setTimeout(push, DEBOUNCE_MS);
}

window.addEventListener("popstate", scheduleSync);
window.addEventListener("hashchange", scheduleSync);
setInterval(scheduleSync, POLL_MS);

$("url-input").value = location.href;
$("url-input").addEventListener("change", (e) => {
  const v = e.target.value.trim();
  if (!v) return;
  try {
    const u = new URL(v, location.href);
    history.replaceState({}, "", u.toString());
    scheduleSync();
  } catch {
    setStatus("bad url", true);
  }
});

push();
