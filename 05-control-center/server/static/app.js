// Control-center bridge UI.
//
// Boot:
//   1. GET /api/slots         → learn pool size, render the chip picker
//      ([auto] [1] [2] …).
//   2. Read ?slot=auto / ?slot=N (1-indexed) from URL. No slot ⇒ auto.
//   3. POST /api/session {slot?} → embed returned viewer_url in <iframe>.
//
// Live:
//   • Watch the address bar (popstate, hashchange, 1 s href poll). On
//     change, debounce ~500 ms and POST /api/session/<id>/params {params}.
//   • Click [auto] / [N]   → set ?slot= in URL, re-lease.
//   • Type in URL input    → updates only the ?url= param (the rest of the
//                            address bar URL stays put).
//   • Click [reset]        → clears every URL param except `desktop` /
//                            `slot` (which stay sticky) and re-syncs.
//   • Click [params (N)]   → toggles a small popup with the current
//                            passthrough JSON.

(() => {
  "use strict";

  const POLL_MS = 1000;
  const DEBOUNCE_MS = 500;

  const $state = document.getElementById("status-state");
  const $err = document.getElementById("status-error");
  const $viewer = document.getElementById("viewer");
  const $placeholder = document.getElementById("placeholder");
  const $url = document.getElementById("url-input");
  const $picker = document.getElementById("slot-picker");
  const $reset = document.getElementById("reset-btn");
  const $paramsBtn = document.getElementById("params-btn");
  const $paramsPopup = document.getElementById("params-popup");
  const $paramsPopupBody = document.getElementById("params-popup-body");
  const $badge = document.getElementById("sync-badge");

  // -- module state ------------------------------------------------------
  let session = null;
  let mode = "auto";          // "auto" | "manual"
  let currentSlot = null;     // 0-based actual slot in use
  let poolSize = 0;
  let lastParams = "";
  let lastSentHref = "";
  let lastParamsJson = "{}";  // latest pretty JSON for popup
  let debounceTimer = null;
  let leasing = false;

  function setState(s) { $state.textContent = s; }
  function setError(s) {
    $err.textContent = s || "";
    if (s) setBadge("err", s);
  }
  function setBadge(kind, hover) {
    $badge.classList.remove("sync-idle", "sync-ok", "sync-pending", "sync-err");
    $badge.classList.add(`sync-${kind}`);
    if (hover !== undefined) $badge.title = hover;
  }
  function setLastSync(ts) { setBadge("ok", `last sync: ${ts}`); }
  function nowStr() { return new Date().toTimeString().slice(0, 8); }

  // -- URL helpers -------------------------------------------------------
  function currentSearchParams() {
    return new URLSearchParams(window.location.search);
  }
  function applyHref(searchParams) {
    const qs = searchParams.toString();
    const next = window.location.pathname + (qs ? "?" + qs : "") + window.location.hash;
    history.replaceState({}, "", next);
  }
  function getUrlParam() {
    const sp = currentSearchParams();
    return sp.get("url") || sp.get("u") || "";
  }
  function setUrlParam(val) {
    const sp = currentSearchParams();
    sp.delete("u");                          // collapse to the canonical key
    if (val) sp.set("url", val);
    else sp.delete("url");
    applyHref(sp);
  }

  // Read params + slot choice from the current address bar.
  // ?url= / ?u= are reserved at the URL level but get promoted into
  // params.open_url so the in-container hook reacts to changes.
  function readLocation() {
    const sp = currentSearchParams();
    const params = {};
    let slotChoice = null;
    let openFromReserved = "";
    for (const [k, v] of sp.entries()) {
      if (k === "desktop") continue;
      if (k === "url" || k === "u") { openFromReserved = v; continue; }
      if (k === "slot") {
        if (v === "auto" || v === "") { slotChoice = "auto"; continue; }
        const n = parseInt(v, 10);
        if (Number.isFinite(n)) slotChoice = n - 1;
        continue;
      }
      params[k] = v;
    }
    if (openFromReserved && !("open_url" in params)) {
      params.open_url = openFromReserved;
    }
    return { params, slotChoice };
  }
  function slotChoiceToTarget(slotChoice) {
    if (slotChoice === null || slotChoice === "auto") return { mode: "auto", idx: null };
    if (slotChoice >= 0 && slotChoice < poolSize) return { mode: "manual", idx: slotChoice };
    return { mode: "auto", idx: null };
  }

  // -- HTTP --------------------------------------------------------------
  async function postJSON(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      let detail = "";
      try { detail = (await r.json()).error || ""; } catch (_) {}
      throw new Error(`${path} -> ${r.status} ${detail}`);
    }
    return r.json();
  }
  async function getJSON(path) {
    const r = await fetch(path, { headers: { "Accept": "application/json" } });
    if (!r.ok) throw new Error(`${path} -> ${r.status}`);
    return r.json();
  }

  // -- picker ------------------------------------------------------------
  function renderPicker() {
    $picker.innerHTML = "";
    const auto = document.createElement("button");
    auto.className = "slot-chip slot-chip-auto";
    auto.type = "button";
    auto.textContent = "auto";
    auto.dataset.idx = "auto";
    auto.title = "auto — round-robin, server picks";
    auto.addEventListener("click", () => onPickAuto());
    $picker.appendChild(auto);
    for (let i = 0; i < poolSize; i++) {
      const b = document.createElement("button");
      b.className = "slot-chip";
      b.type = "button";
      b.textContent = String(i + 1);
      b.dataset.idx = String(i);
      b.title = `pin to slot ${i + 1}`;
      b.addEventListener("click", () => onPickManual(i));
      $picker.appendChild(b);
    }
    refreshPickerActive();
  }
  function refreshPickerActive() {
    for (const el of $picker.querySelectorAll(".slot-chip")) {
      const isAuto = el.dataset.idx === "auto";
      const idx = isAuto ? null : Number(el.dataset.idx);
      const isActive = isAuto ? (mode === "auto") : (mode === "manual" && idx === currentSlot);
      const isCurrentLease = !isAuto && mode === "auto" && idx === currentSlot;
      el.classList.toggle("active", isActive);
      el.classList.toggle("current-lease", isCurrentLease);
    }
  }

  async function onPickAuto() {
    if (leasing || mode === "auto") return;
    const sp = currentSearchParams();
    sp.set("slot", "auto");
    applyHref(sp);
    refreshUrlInput();
    lastSentHref = window.location.href;
    await leaseSlot({ mode: "auto", idx: null });
  }
  async function onPickManual(idx) {
    if (leasing) return;
    if (mode === "manual" && idx === currentSlot) return;
    const sp = currentSearchParams();
    sp.set("slot", String(idx + 1));
    applyHref(sp);
    refreshUrlInput();
    lastSentHref = window.location.href;
    await leaseSlot({ mode: "manual", idx });
  }

  // -- lease -------------------------------------------------------------
  async function leaseSlot({ mode: nextMode, idx }) {
    if (leasing) return;
    leasing = true;
    setState(session ? "switching slot…" : "spawning…");
    setBadge("pending", "leasing…");
    setError("");
    try {
      const { params } = readLocation();
      const body = (nextMode === "manual") ? { slot: idx, params } : { params };
      const data = await postJSON("/api/session", body);
      session = data.session_id;
      mode = nextMode;
      currentSlot = (typeof data.slot === "number") ? data.slot : idx;
      $viewer.src = data.viewer_url;
      $viewer.style.display = "block";
      $placeholder.style.display = "none";
      updateParamsDisplay(params);
      lastParams = JSON.stringify(params);
      lastSentHref = window.location.href;
      setLastSync(nowStr());
      setState("connected");
      refreshPickerActive();
    } catch (e) {
      console.error(e);
      setState("lease failed");
      setError(String(e.message || e));
    } finally {
      leasing = false;
    }
  }

  // -- URL → param sync --------------------------------------------------
  async function pushParams() {
    if (!session) return;
    const { params, slotChoice } = readLocation();
    const target = slotChoiceToTarget(slotChoice);
    const slotChanged =
      (target.mode !== mode) ||
      (target.mode === "manual" && target.idx !== currentSlot);
    if (slotChanged) {
      await leaseSlot(target);
      return;
    }
    const key = JSON.stringify(params);
    if (key === lastParams) return;
    lastParams = key;
    setState("syncing…");
    setBadge("pending", "syncing…");
    try {
      await postJSON(`/api/session/${session}/params`, { params });
      updateParamsDisplay(params);
      setLastSync(nowStr());
      setState("connected");
      setError("");
    } catch (e) {
      console.error(e);
      setState("connected");
      setError(String(e.message || e));
    }
  }
  function scheduleSync() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(pushParams, DEBOUNCE_MS);
  }

  // -- URL input (?url= editor) -----------------------------------------
  function refreshUrlInput() {
    if (document.activeElement === $url) return;
    const v = getUrlParam();
    if ($url.value !== v) {
      $url.value = v;
      $url.classList.remove("dirty");
    }
  }
  $url.value = getUrlParam();
  $url.addEventListener("input", () => $url.classList.add("dirty"));
  $url.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $url.blur(); applyUrlInput(); }
    else if (e.key === "Escape") {
      e.preventDefault();
      $url.value = getUrlParam();
      $url.classList.remove("dirty");
      $url.blur();
    }
  });
  let blurTimer = null;
  $url.addEventListener("blur", () => {
    if (blurTimer) clearTimeout(blurTimer);
    blurTimer = setTimeout(applyUrlInput, 0);
  });
  function applyUrlInput() {
    const v = $url.value.trim();
    if (v === getUrlParam()) { $url.classList.remove("dirty"); return; }
    setUrlParam(v);
    $url.classList.remove("dirty");
    setError("");
    lastSentHref = window.location.href;
    scheduleSync();
  }

  // -- reset button ------------------------------------------------------
  $reset.addEventListener("click", () => {
    const sp = currentSearchParams();
    const keep = new Set(["desktop", "slot"]);
    for (const k of [...sp.keys()]) {
      if (!keep.has(k)) sp.delete(k);
    }
    applyHref(sp);
    refreshUrlInput();
    setError("");
    lastSentHref = window.location.href;
    scheduleSync();
  });

  // -- params popup ------------------------------------------------------
  function updateParamsDisplay(p) {
    const count = Object.keys(p).length;
    $paramsBtn.textContent = `params (${count})`;
    lastParamsJson = JSON.stringify(p, null, 2);
    if (!$paramsPopup.hidden) $paramsPopupBody.textContent = lastParamsJson;
  }
  $paramsBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if ($paramsPopup.hidden) {
      $paramsPopupBody.textContent = lastParamsJson;
      $paramsPopup.hidden = false;
      $paramsBtn.setAttribute("aria-expanded", "true");
    } else {
      $paramsPopup.hidden = true;
      $paramsBtn.setAttribute("aria-expanded", "false");
    }
  });
  // Click anywhere outside the popup or the button closes it.
  document.addEventListener("click", (e) => {
    if ($paramsPopup.hidden) return;
    if ($paramsPopup.contains(e.target) || $paramsBtn.contains(e.target)) return;
    $paramsPopup.hidden = true;
    $paramsBtn.setAttribute("aria-expanded", "false");
  });

  // -- watchers ----------------------------------------------------------
  window.addEventListener("popstate", () => { refreshUrlInput(); scheduleSync(); });
  window.addEventListener("hashchange", () => { refreshUrlInput(); scheduleSync(); });
  setInterval(() => {
    if (window.location.href !== lastSentHref) {
      lastSentHref = window.location.href;
      refreshUrlInput();
      scheduleSync();
    } else {
      refreshUrlInput();
    }
  }, POLL_MS);

  // -- boot --------------------------------------------------------------
  (async function boot() {
    try {
      const info = await getJSON("/api/slots");
      poolSize = info.pool_size || (info.slots || []).length;
      if (!poolSize) {
        setState("no slots");
        setError("pool is empty");
        return;
      }
      renderPicker();
      const { slotChoice } = readLocation();
      await leaseSlot(slotChoiceToTarget(slotChoice));
    } catch (e) {
      console.error(e);
      setState("boot failed");
      setError(String(e.message || e));
    }
  })();
})();
