"use strict";
(() => {
"use strict";

const $ = (id) => document.getElementById(id);
const VIEW_MODE = location.pathname.replace(/\/+$/, "") === "/view";
const SAVE_PATH_KEY = "annas_save_path_v2";
const PREALLOCATE_KEY = "annas_preallocate";
const SORT_KEY = "annas_torrent_sort";
const FILTER_KEY = "annas_torrent_filter";
const MASTODON_KEY = "mastodonInstance";
const API_TOKEN_KEY = "annas_api_token";
// Decimal Mbps: 1_000_000 bit/s = 125_000 B/s.
const RATE_BPS_PER_MBPS = 125000;
const RATE_PRESETS = [1, 5, 10, 25, 50];
const FILTER_KEYS = new Set(["all", "downloading", "seeding", "paused", "error"]);
const SORT_KEYS = new Set(["name", "state", "progress", "size", "download_rate", "upload_rate", "num_seeds", "num_peers"]);

const STATE_LABELS = {
  downloading: "Downloading",
  seeding: "Seeding",
  paused: "Paused",
  stalled: "Waiting",
  queued: "Queued",
  checking: "Checking",
  error: "Error",
  missing_files: "Missing files",
  allocating: "Allocating",
  moving: "Moving",
  unknown: "Unknown",
};

let appConfig = { public_url: "", backend: "libtorrent", qbit_category: "", defaults: {} };
let publicBase = ""; // only set when PUBLIC_URL is configured
let latestCoverage = null;
let lastSnapshot = null;
let spacePreview = null;
let spaceRequestId = 0;
let storageRequestId = 0;
let liveEs = null;
let pendingShare = null;
let settingsFocusBefore = null;
let shareFocusBefore = null;
let pendingRemove = null; // { infohash, name, size }
let removeFocusBefore = null;
const memoryStorage = new Map();
function storedGet(key) {
  try { return localStorage.getItem(key); } catch { return memoryStorage.get(key) || null; }
}
function storedSet(key, value) {
  try { localStorage.setItem(key, value); } catch { memoryStorage.set(key, String(value)); }
}
function storedRemove(key) {
  try { localStorage.removeItem(key); } catch { memoryStorage.delete(key); }
}

let torrentFilter = FILTER_KEYS.has(storedGet(FILTER_KEY) || "") ? storedGet(FILTER_KEY) : "all";
let lastImpactBytes = null;
let lastMetricText = {};
let syncingControls = false;
let controlsMsgTimer = null;
let controlQueue = Promise.resolve();
let connectGeneration = 0;
let reconnectTimer = null;
let statusAbort = null;
let authBlocked = false;
let provisionInFlight = false;
let spaceFreeId = 0;

function stillCurrent(generation) {
  return generation === connectGeneration && !authBlocked;
}

function formatApiDetail(detail, fallback) {
  if (detail == null || detail === "") return fallback || "Request failed";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (d && typeof d === "object" ? d.msg || d.message || JSON.stringify(d) : String(d)))
      .filter(Boolean)
      .join("; ") || fallback || "Request failed";
  }
  if (typeof detail === "object") return detail.msg || detail.message || JSON.stringify(detail);
  return String(detail);
}

function resetControlsChrome() {
  syncingControls = true;
  try {
    for (const id of ["pause-btn", "resume-btn", "pause-dl-btn", "resume-dl-btn"]) {
      const el = $(id);
      if (el) {
        el.classList.remove("is-active");
        el.disabled = true;
      }
    }
    for (const id of ["upload-limit", "download-limit", "upload-custom-mbps", "download-custom-mbps"]) {
      const el = $(id);
      if (el) {
        el.disabled = true;
        if (el.tagName === "SELECT") el.value = "-1";
      }
    }
    for (const id of ["upload-custom-wrap", "download-custom-wrap"]) {
      const el = $(id);
      if (el) el.hidden = true;
    }
    const section = $("global-controls");
    if (section) section.setAttribute("aria-disabled", "true");
  } finally {
    syncingControls = false;
  }
}

function invalidateConnection() {
  connectGeneration++;
  // Unlock provision / abort in-flight free UX when Settings or reconnect invalidates.
  provisionInFlight = false;
  spaceFreeId++;
  if (liveEs) {
    try { liveEs.close(); } catch { /* ignore */ }
    liveEs = null;
  }
  if (statusAbort) {
    try { statusAbort.abort(); } catch { /* ignore */ }
    statusAbort = null;
  }
}

function apiHeaders(withJson) {
  const h = {};
  if (withJson) h["Content-Type"] = "application/json";
  const t = storedGet(API_TOKEN_KEY);
  if (t) h["X-API-Token"] = t;
  return h;
}

function authFailure() {
  authBlocked = true;
  // Invalidate in-flight private responses (storage/space/controls/remove/pick).
  // invalidateConnection clears provisionInFlight and bumps spaceFreeId + generation.
  invalidateConnection();
  storageRequestId++;
  spaceRequestId++;
  lastSnapshot = null;
  latestCoverage = null;
  lastImpactBytes = null;
  lastMetricText = {};
  appConfig = {
    ...appConfig,
    qbit_url: "",
    qbit_user: "",
    qbit_category: "",
    qbit_pass_set: false,
  };
  const sel = $("save_path");
  if (sel) sel.replaceChildren();
  setConnPill("degraded", "Authentication failed");
  toast("API token rejected — open Settings and enter a valid token");
  for (const id of ["m-up", "m-down", "m-disk", "m-torrents", "m-peers", "m-totup", "m-storage"]) {
    const el = $(id);
    if (el) el.textContent = "—";
  }
  const impact = $("impact-lead");
  if (impact) impact.textContent = "Authentication required";
  for (const id of ["imp-storage", "imp-torrents", "imp-pct", "imp-upload"]) {
    const el = $(id);
    if (el) el.textContent = "—";
  }
  const bar = $("cov-bar");
  if (bar) bar.style.width = "0%";
  const rows = $("torrent-rows");
  if (rows) {
    rows.replaceChildren();
    rows.appendChild(emptyRow(VIEW_MODE ? 8 : 9, "Authentication required"));
  }
  const shareGate = $("share-gate");
  const shareActive = $("share-active");
  if (shareGate) shareGate.hidden = false;
  if (shareActive) shareActive.hidden = true;
  const provisionBtn = $("provision-btn");
  if (provisionBtn) provisionBtn.disabled = true;
  resetControlsChrome();
  clearSpacePreview();
}

function fetchWithTimeout(url, opts = {}, ms = 15000) {
  const ctrl = new AbortController();
  const outer = opts.signal;
  const onAbort = () => ctrl.abort();
  if (outer) {
    if (outer.aborted) ctrl.abort();
    else outer.addEventListener("abort", onAbort, { once: true });
  }
  const t = setTimeout(() => ctrl.abort(), ms);
  return fetch(url, { ...opts, signal: ctrl.signal }).finally(() => {
    clearTimeout(t);
    if (outer) outer.removeEventListener("abort", onAbort);
  });
}

function apiFetch(url, opts = {}) {
  const timeoutMs = opts.timeoutMs != null ? opts.timeoutMs : 15000;
  const { timeoutMs: _ignored, ...rest } = opts;
  const next = { ...rest };
  const withJson = !!(opts.body && typeof opts.body === "string");
  next.headers = { ...apiHeaders(withJson), ...(opts.headers || {}) };
  const generation = connectGeneration;
  return fetchWithTimeout(url, next, timeoutMs).then(async (r) => {
    // Ignore stale 401/503s from requests started before a successful re-auth.
    if (generation !== connectGeneration) return r;
    if (
      r.status === 401 &&
      !VIEW_MODE &&
      !String(url).includes("/public/")
    ) {
      authFailure();
    }
    // Only treat missing server token as auth failure — other 503s (backend
    // changed, SSE caps) must reconnect, not wipe private UI.
    if (r.status === 503 && !VIEW_MODE && !String(url).includes("/public/")) {
      let detail = "";
      try {
        detail = String((await r.clone().json())?.detail || "");
      } catch {
        detail = "";
      }
      if (detail === "API_TOKEN must be configured") {
        authFailure();
        setConnPill("degraded", "Server token missing");
      }
    }
    return r;
  });
}

function eventsUrl(path, ticket) {
  if (!ticket || path.includes("/public/")) return path;
  return path + (path.includes("?") ? "&" : "?") + "ticket=" + encodeURIComponent(ticket);
}

function clearSpacePreview() {
  spaceRequestId++;
  spaceFreeId++;
  spacePreview = null;
  const wrap = $("space-preview");
  if (wrap) wrap.hidden = true;
  const btn = $("space-confirm-btn");
  if (btn) btn.disabled = true;
  const msg = $("space-msg");
  if (msg) {
    msg.textContent = "";
    msg.classList.remove("error", "ok");
  }
}

function updateSpaceDestLabel() {
  const el = $("space-dest-label");
  if (!el) return;
  const sel = $("save_path");
  const opt = sel?.selectedOptions?.[0];
  const path = sel?.value || "";
  el.textContent = path
    ? (opt?.textContent || path)
    : "not selected";
}

function safeParse(raw, fallback) {
  try {
    if (!raw) return fallback;
    const v = JSON.parse(raw);
    return v == null ? fallback : v;
  } catch {
    return fallback;
  }
}

let sort = safeParse(storedGet(SORT_KEY), { key: "state", dir: "asc" });
if (!sort || !SORT_KEYS.has(sort.key) || (sort.dir !== "asc" && sort.dir !== "desc")) {
  sort = { key: "state", dir: "asc" };
}

/** Decimal SI units; GB/TB always 2 decimals. */
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 0) n = 0;
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  let v = n;
  while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
  if (i >= 3) return v.toFixed(2) + " " + u[i]; // GB+
  if (i === 0) return Math.round(v) + " B";
  return (v < 10 ? v.toFixed(2) : v.toFixed(v < 100 ? 1 : 0)) + " " + u[i];
}
function fmtRate(n) {
  return fmtBytes(n) + "/s";
}
function fmtSwarm(connected, total) {
  const c = connected == null ? 0 : Number(connected);
  if (total == null || total === "" || Number(total) < 0) return String(Number.isFinite(c) ? c : "—");
  return `${Number.isFinite(c) ? c : 0} (${Number(total)})`;
}

function roundImpact(bytes, pct) {
  const size = fmtBytes(bytes);
  let pctStr = "";
  if (pct != null && Number.isFinite(pct)) {
    const r = pct < 0.01 ? pct.toFixed(4) : pct < 1 ? pct.toFixed(3) : pct.toFixed(2);
    pctStr = r + "%";
  }
  return { size, pct: pctStr };
}

function connectionFrom(s) {
  if (!s) return "offline";
  const raw = (s.connection || s.global?.connection || "").toLowerCase();
  if (raw === "connected" || raw === "degraded" || raw === "offline") return raw;
  if (s.global && s.global.backend_ok === false) return "degraded";
  if (s.global && s.global.backend_ok === true) return "connected";
  return "offline";
}

function setConnPill(state, label) {
  const el = $("conn-pill");
  el.className = "conn-pill " + state;
  el.textContent = label || ({
    connected: "Connected",
    degraded: "Backend issue",
    offline: "Offline",
    reconnecting: "Reconnecting…",
  }[state] || state);
}

function torrentStateKey(t) {
  if (t.paused) return "paused";
  if (t.is_seeding) return "seeding";
  const s = String(t.state || "unknown").toLowerCase();
  if (s.includes("missing")) return "missing_files";
  if (s.includes("check")) return "checking";
  if (s.includes("queue")) return "queued";
  if (s.includes("stall")) return "stalled";
  if (s.includes("paus") || s.includes("stop")) return "paused";
  if (s.includes("download")) return "downloading";
  if (s.includes("seed") || s.includes("upload")) return "seeding";
  if (s.includes("error")) return "error";
  return s || "unknown";
}

function humanState(t) {
  const key = torrentStateKey(t);
  return STATE_LABELS[key] || (key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, " "));
}

function pillClass(key) {
  if (key === "missing_files") return "pill-error";
  if (["downloading", "seeding", "paused", "stalled", "queued", "checking", "error"].includes(key))
    return "pill-" + key;
  return "";
}

function flashIfChanged(el, text) {
  if (!el) return;
  const key = el.id || text;
  const prev = lastMetricText[key];
  el.textContent = text;
  if (prev != null && prev !== text) {
    el.classList.remove("flash");
    void el.offsetWidth;
    el.classList.add("flash");
  }
  lastMetricText[key] = text;
}

function rateSelectValue(bps) {
  if (bps == null || bps < 0) return "-1";
  for (const mbps of RATE_PRESETS) {
    if (Math.abs(bps - mbps * RATE_BPS_PER_MBPS) <= 1) return String(mbps);
  }
  return "custom";
}

function syncControlsFromSnapshot(ctrl) {
  if (!ctrl || syncingControls) return;
  syncingControls = true;
  try {
    $("pause-btn").classList.toggle("is-active", !!ctrl.seeding_paused);
    $("resume-btn").classList.toggle("is-active", !ctrl.seeding_paused);
    $("pause-dl-btn").classList.toggle("is-active", !!ctrl.downloads_paused);
    $("resume-dl-btn").classList.toggle("is-active", !ctrl.downloads_paused);

    for (const kind of ["upload", "download"]) {
      const bps = kind === "upload" ? ctrl.upload_limit : ctrl.download_limit;
      const sel = rateSelectValue(bps);
      const select = $(kind + "-limit");
      if (document.activeElement === select || document.activeElement === $(kind + "-custom-mbps")) continue;
      select.value = sel === "custom" || RATE_PRESETS.map(String).includes(sel) || sel === "-1" ? sel : "custom";
      if (![...select.options].some((o) => o.value === select.value)) select.value = "custom";
      const custom = select.value === "custom";
      $(kind + "-custom-wrap").hidden = !custom;
      if (custom && bps > 0) {
        $(kind + "-custom-mbps").value = String(Math.round((bps / RATE_BPS_PER_MBPS) * 10) / 10);
      }
    }
  } finally {
    syncingControls = false;
  }
}

function setControlsMsg(text, ok) {
  const msg = $("controls-msg");
  msg.textContent = text || "";
  msg.classList.toggle("error", !!text && !ok);
  msg.classList.toggle("ok", !!text && !!ok);
  if (controlsMsgTimer) clearTimeout(controlsMsgTimer);
  if (text && ok) {
    controlsMsgTimer = setTimeout(() => {
      if (msg.textContent === text) {
        msg.textContent = "";
        msg.classList.remove("ok");
      }
    }, 2500);
  }
}

function hasVerifiedContribution(s) {
  // Live indexed contribution only — history must not unlock sharing of "0 B".
  const c = s?.coverage;
  return !!(c && c.index_ready && Number(c.seeded_bytes) > 0);
}

function updateShareGate(s) {
  const ok = hasVerifiedContribution(s);
  $("share-gate").hidden = ok;
  $("share-active").hidden = !ok;
  const note = $("share-public-note");
  if (!ok) return;
  if (publicBase) {
    note.textContent =
      "Shares include a public /view link showing community impact only (no paths, hashes, settings, or host free space). " +
      "Preview appears before any social network opens.";
  } else {
    note.textContent = "Preview appears before any social network opens.";
  }
}

function sortValue(t, key) {
  if (key === "name") return (t.name || t.infohash || "").toLowerCase();
  if (key === "state") return humanState(t).toLowerCase();
  // Sort Seeds/Peers by swarm total when known (matches qBit column feel).
  if (key === "num_seeds") {
    const tot = t.seeds_total;
    return tot != null && Number(tot) >= 0 ? Number(tot) : Number(t.num_seeds) || 0;
  }
  if (key === "num_peers") {
    const tot = t.peers_total;
    return tot != null && Number(tot) >= 0 ? Number(tot) : Number(t.num_peers) || 0;
  }
  return Number(t[key]) || 0;
}

function sortedTorrents(rows) {
  const dir = sort.dir === "desc" ? -1 : 1;
  return [...rows].sort((a, b) => {
    const va = sortValue(a, sort.key), vb = sortValue(b, sort.key);
    if (va < vb) return -1 * dir;
    if (va > vb) return 1 * dir;
    return 0;
  });
}

function paintSortHeaders() {
  document.querySelectorAll("#torrent-head th[scope='col']").forEach((th) => {
    const btn = th.querySelector(".sort-btn");
    if (!btn) return;
    if (btn.dataset.key === sort.key) {
      th.setAttribute("aria-sort", sort.dir === "asc" ? "ascending" : "descending");
      const ind = btn.querySelector(".ind");
      if (ind) ind.textContent = sort.dir === "asc" ? "▲" : "▼";
    } else {
      th.setAttribute("aria-sort", "none");
      const ind = btn.querySelector(".ind");
      if (ind) ind.textContent = "▲";
    }
  });
}

function filterTorrents(rows) {
  const list = rows || [];
  if (torrentFilter === "all") return list;
  return list.filter((t) => {
    const key = torrentStateKey(t);
    if (torrentFilter === "downloading") {
      return key === "downloading" || key === "stalled" || key === "checking"
        || key === "queued" || key === "allocating" || key === "moving";
    }
    if (torrentFilter === "seeding") return key === "seeding";
    if (torrentFilter === "paused") return key === "paused";
    if (torrentFilter === "error") return key === "error" || key === "missing_files";
    return true;
  });
}

function paintFilterButtons() {
  document.querySelectorAll("#torrent-filters .filter-btn").forEach((btn) => {
    btn.setAttribute("aria-pressed", btn.dataset.filter === torrentFilter ? "true" : "false");
  });
}

function emptyRow(colspan, text) {
  const tr = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = colspan;
  td.className = "sub";
  td.textContent = text;
  tr.appendChild(td);
  return tr;
}

function renderTorrents(rows) {
  const tbody = $("torrent-rows");
  tbody.replaceChildren();
  const all = rows || [];
  const filtered = filterTorrents(all);
  const list = sortedTorrents(filtered);
  const cols = VIEW_MODE ? 8 : 9;
  const countEl = $("torrent-filter-count");
  if (countEl) {
    countEl.textContent =
      torrentFilter === "all"
        ? `${all.length} torrent${all.length === 1 ? "" : "s"}`
        : `${list.length} of ${all.length}`;
  }
  if (!all.length) {
    const n = lastSnapshot && lastSnapshot.global ? Number(lastSnapshot.global.num_torrents) || 0 : 0;
    const emptyText =
      VIEW_MODE && n > 0
        ? `${n} active torrent${n === 1 ? "" : "s"} (details hidden on public view)`
        : "No torrents yet.";
    tbody.appendChild(emptyRow(cols, emptyText));
    return;
  }
  if (!list.length) {
    tbody.appendChild(emptyRow(cols, "No torrents match this filter."));
    return;
  }
  for (const t of list) {
    const tr = document.createElement("tr");
    const nameTd = document.createElement("td");
    nameTd.className = "col-name";
    const name = t.name || (t.infohash ? t.infohash.slice(0, 12) : "—");
    nameTd.textContent = name;
    nameTd.title = name;
    if (t.infohash && !VIEW_MODE) nameTd.title = name + "\n" + t.infohash;

    const stateTd = document.createElement("td");
    stateTd.className = "col-state";
    const key = torrentStateKey(t);
    const pill = document.createElement("span");
    pill.className = "pill " + pillClass(key);
    pill.textContent = humanState(t);
    if (key === "missing_files") pill.title = "Files are missing on disk";
    if (key === "error") pill.title = "Torrent reported an error";
    stateTd.appendChild(pill);

    const progressPct = ((t.progress || 0) * 100).toFixed(1) + "%";
    const progressTd = numCell(progressPct);
    progressTd.classList.add("col-progress");
    const done = t.downloaded != null ? Number(t.downloaded) : Math.round((t.size || 0) * (t.progress || 0));
    progressTd.title = `${fmtBytes(done)} of ${fmtBytes(t.size)} on disk`;

    const sizeTd = numCell(fmtBytes(t.size));
    sizeTd.classList.add("col-size");
    sizeTd.title = "Total torrent size";
    const downTd = numCell(fmtRate(t.download_rate));
    downTd.classList.add("col-down");
    const rateTd = numCell(fmtRate(t.upload_rate));
    rateTd.classList.add("col-rate");
    const seedsTd = numCell(fmtSwarm(t.num_seeds, t.seeds_total));
    seedsTd.classList.add("col-seeds");
    const peersTd = numCell(fmtSwarm(t.num_peers, t.peers_total));
    peersTd.classList.add("col-peers");

    const cells = [nameTd, stateTd, progressTd, sizeTd, downTd, rateTd, seedsTd, peersTd];
    for (const c of cells) tr.appendChild(c);

    if (!VIEW_MODE) {
      const act = document.createElement("td");
      act.className = "row-actions";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ghost";
      btn.textContent = "Remove";
      btn.setAttribute("aria-label", `Remove ${name}`);
      btn.disabled = !t.infohash;
      btn.addEventListener("click", () => openRemoveModal(t));
      act.appendChild(btn);
      tr.appendChild(act);
    }
    tbody.appendChild(tr);
  }
}

function numCell(text) {
  const td = document.createElement("td");
  td.className = "num";
  td.textContent = text;
  return td;
}

function render(s) {
  lastSnapshot = s;
  const g = s.global || {};
  const c = s.coverage || {};
  const p = s.provision || {};
  const ctrl = s.controls || {};
  const torrents = s.torrents || [];

  setConnPill(connectionFrom(s));
  if (connectionFrom(s) === "connected") {
    const section = $("global-controls");
    if (section) section.removeAttribute("aria-disabled");
    for (const id of [
      "pause-btn", "resume-btn", "pause-dl-btn", "resume-dl-btn",
      "upload-limit", "download-limit", "upload-custom-mbps", "download-custom-mbps",
    ]) {
      const el = $(id);
      if (el) el.disabled = false;
    }
    syncControlsFromSnapshot(ctrl);
  } else {
    resetControlsChrome();
  }

  flashIfChanged($("m-up"), fmtRate(g.upload_rate));
  flashIfChanged($("m-down"), fmtRate(g.download_rate));
  const disk = $("m-disk");
  const diskText = fmtBytes(g.committed_bytes);
  const prevDisk = lastMetricText["m-disk-main"];
  disk.replaceChildren();
  const diskMain = document.createElement("span");
  diskMain.textContent = diskText;
  disk.append(diskMain);
  const small = document.createElement("small");
  if (s.public) {
    // /view omits host free capacity on purpose.
    small.textContent = "";
  } else if (g.disk_free_known === false) {
    small.textContent = " / free space unknown";
  } else {
    small.textContent = " / " + fmtBytes(g.disk_free) + " free";
  }
  if (small.textContent) {
    disk.append(" ");
    disk.append(small);
  }
  if (prevDisk != null && prevDisk !== diskText) {
    disk.classList.remove("flash");
    void disk.offsetWidth;
    disk.classList.add("flash");
  }
  lastMetricText["m-disk-main"] = diskText;
  $("m-storage").textContent =
    "New downloads go to: " + (g.storage_path || "client's configured location");
  flashIfChanged($("m-torrents"), String(g.num_torrents ?? "–"));
  flashIfChanged($("m-peers"), String(g.num_peers ?? "–"));
  flashIfChanged($("m-totup"), fmtBytes(g.total_upload));

  latestCoverage = c;
  const covBytes = Number(c.seeded_bytes) || 0;
  const localBytes = Number(g.committed_bytes) || 0;
  // Impact copy is archive contribution only; disk card still shows localBytes.
  const displayBytes = covBytes > 0 ? covBytes : 0;
  const { size, pct } = roundImpact(
    displayBytes,
    covBytes > 0 && c.index_ready ? c.percent : null
  );
  const complete = torrents.filter((t) => t.is_complete && torrentStateKey(t) !== "missing_files").length;
  const incomplete = torrents.length - complete;
  const publicMode = !!s.public;

  if (c.index_ready && covBytes > 0) {
    const lead = `You are helping preserve ${size}` + (pct ? ` (${pct})` : "") + " of Anna's Archive";
    const leadEl = $("impact-lead");
    if (lastImpactBytes != null && displayBytes > lastImpactBytes) {
      leadEl.classList.remove("flash");
      void leadEl.offsetWidth;
      leadEl.classList.add("flash");
    }
    leadEl.textContent = lead;
    flashIfChanged($("imp-storage"), size);
    flashIfChanged($("imp-pct"), pct || "–");
    const bar = $("cov-bar");
    const w = Math.min(100, Number(c.percent) || 0);
    bar.style.width = w + "%";
    $("cov-bar-wrap").setAttribute("aria-valuenow", String(w));
  } else if (c.index_ready) {
    $("impact-lead").textContent = "You are helping preserve – of Anna's Archive";
    $("imp-storage").textContent = localBytes > 0 ? fmtBytes(localBytes) : "…";
    $("imp-pct").textContent = "–";
    $("cov-bar").style.width = "0%";
    $("cov-bar-wrap").setAttribute("aria-valuenow", "0");
  } else {
    $("impact-lead").textContent = "You are helping preserve – of Anna's Archive";
    $("imp-storage").textContent = "…";
    $("imp-pct").textContent = "loading…";
  }
  lastImpactBytes = displayBytes;
  flashIfChanged($("imp-torrents"), String(g.num_torrents ?? "–"));
  if (publicMode) {
    $("imp-torrents-hint").textContent = "Active torrents";
  } else {
    $("imp-torrents-hint").textContent = `${complete} complete · ${incomplete} incomplete`;
  }
  $("imp-storage-hint").textContent = covBytes > 0 ? "Indexed contribution" : (localBytes > 0 ? "Local content (not indexed)" : "Content size");
  flashIfChanged($("imp-upload"), fmtBytes(g.total_upload));

  paintSortHeaders();
  paintFilterButtons();
  renderTorrents(torrents);

  // Provision status
  const msg = $("provision-msg");
  const detail = $("provision-detail");
  if (p.running) {
    const phase = String(p.phase || "working");
    const phaseLabel = {
      selecting: "Selecting torrents",
      downloading: "Downloading metadata",
      adding: "Adding torrents",
      working: "Working",
    }[phase] || phase;
    msg.textContent = p.message || "Working…";
    msg.classList.remove("error", "ok");
    detail.hidden = false;
    const parts = [phaseLabel];
    if (p.requested_tb) parts.push(`target ${p.requested_tb} TB`);
    if (p.selected_bytes) parts.push(`selected ${fmtBytes(p.selected_bytes)}`);
    parts.push(`added ${p.added || 0}`);
    if (p.failed) parts.push(`failed ${p.failed}`);
    detail.textContent = parts.join(" · ");
  } else if (p.phase === "error") {
    msg.textContent = p.message || "Something went wrong";
    msg.classList.remove("ok");
    msg.classList.add("error");
    detail.hidden = false;
    detail.textContent = "Contribution stopped with an error. Adjust the amount or destination and try again.";
  } else if (p.phase === "done") {
    const added = Number(p.added) || 0;
    const failed = Number(p.failed) || 0;
    const success = added > 0;
    msg.textContent = success
      ? (p.message || "Done") + " — adding stopped; seeding continues."
      : (p.message || "Nothing was added") + (failed ? " — try again." : ".");
    msg.classList.remove("error", "ok");
    if (success) msg.classList.add("ok");
    else if (failed > 0) msg.classList.add("error");
    detail.hidden = false;
    detail.textContent =
      `Finished · added ${added}` +
      (failed ? ` · failed ${failed}` : "") +
      (p.selected_bytes ? ` · selected ${fmtBytes(p.selected_bytes)}` : "");
  } else if (p.message && p.message !== "idle") {
    msg.textContent = p.message;
    msg.classList.remove("error", "ok");
    detail.hidden = true;
  } else {
    msg.textContent = "";
    msg.classList.remove("error", "ok");
    detail.hidden = true;
  }
  if (!provisionInFlight) {
    $("provision-btn").disabled = false;
    $("provision-btn").textContent = p.running ? "Cancel contribution" : "Start contributing";
    $("provision-btn").classList.toggle("danger-btn", !!p.running);
    $("provision-btn").classList.toggle("primary", !p.running);
  }

  updateShareGate(s);
}

// --- Storage destinations ---
function ensureSaveOption(path, label) {
  const sel = $("save_path");
  const prev = sel.value;
  if (![...sel.options].some((o) => o.value === path)) {
    const opt = document.createElement("option");
    opt.value = path;
    opt.textContent = label || path;
    sel.appendChild(opt);
  }
  sel.value = path;
  storedSet(SAVE_PATH_KEY, path);
  // Programmatic select changes do not fire `change` — drop stale space preview.
  if (path !== prev) clearSpacePreview();
}

function loadStorageOptions() {
  const sel = $("save_path");
  const requestId = ++storageRequestId;
  const beforePath = sel.value;
  apiFetch("/api/storage")
    .then((r) => {
      if (requestId !== storageRequestId || authBlocked) return null;
      if (!r.ok) throw new Error("storage " + r.status);
      return r.json();
    })
    .then((data) => {
      if (!data || requestId !== storageRequestId || authBlocked) return;
      const opts = data.options || [];
      sel.replaceChildren();
      if (!opts.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = data.default || "No destinations configured";
        sel.appendChild(opt);
        if (beforePath) clearSpacePreview();
        updateSpaceDestLabel();
        return;
      }
      for (const o of opts) {
        const opt = document.createElement("option");
        opt.value = o.path;
        const base = o.label || o.path;
        if (o.disk_free == null) {
          opt.textContent = base;
        } else {
          opt.textContent = `${base} — ${fmtBytes(o.disk_free)} free`;
        }
        opt.title = opt.textContent;
        sel.appendChild(opt);
      }
      const prev = storedGet(SAVE_PATH_KEY);
      const activePath = data.active || "";
      const inUse =
        (activePath && opts.find((o) => o.path === activePath)) ||
        opts.find((o) => String(o.label || "").startsWith("In use"));
      // Prefer where torrents already live over an empty Default content folder.
      // Only select a previously stored path when it is still in the server allowlist.
      if (inUse && (!prev || prev === data.default || ![...sel.options].some((o) => o.value === prev))) {
        sel.value = inUse.path;
        storedSet(SAVE_PATH_KEY, inUse.path);
      } else if (prev && [...sel.options].some((o) => o.value === prev)) {
        sel.value = prev;
      } else if (data.default) {
        sel.value = data.default;
        if (prev) storedSet(SAVE_PATH_KEY, data.default);
      }
      if (sel.value !== beforePath) clearSpacePreview();
      updateSpaceDestLabel();
      const browse = $("browse-btn");
      if (browse) browse.hidden = isQbit(data.backend || appConfig.backend);
    })
    .catch(() => {
      if (requestId !== storageRequestId) return;
      sel.replaceChildren();
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "Could not load destinations — restart the server";
      sel.appendChild(opt);
      clearSpacePreview();
      updateSpaceDestLabel();
    });
}

$("save_path").addEventListener("change", () => {
  storedSet(SAVE_PATH_KEY, $("save_path").value);
  storageRequestId++; // drop in-flight /api/storage that would overwrite the pick
  clearSpacePreview();
  updateSpaceDestLabel();
});

$("preallocate").checked = storedGet(PREALLOCATE_KEY) === "1";
$("preallocate").addEventListener("change", () => {
  storedSet(PREALLOCATE_KEY, $("preallocate").checked ? "1" : "0");
});

$("browse-btn").addEventListener("click", async () => {
  const btn = $("browse-btn");
  const label = $("browse-label");
  const msg = $("provision-msg");
  btn.disabled = true;
  label.textContent = "Waiting…";
  try {
    const r = await apiFetch("/api/storage/pick", {
      method: "POST",
      timeoutMs: 120000,
    });
    if (!r.ok) throw new Error("pick");
    const data = await r.json();
    if (data.path) ensureSaveOption(data.path, data.path);
    else if (data.cancelled) { /* user cancelled */ }
    else {
      msg.textContent = "Folder picker returned no path.";
      msg.classList.remove("ok");
      msg.classList.add("error");
    }
  } catch {
    msg.textContent = "Could not open the folder picker.";
    msg.classList.remove("ok");
    msg.classList.add("error");
  } finally {
    btn.disabled = false;
    label.textContent = "Browse…";
  }
});

$("provision-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("provision-msg");
  const generation = connectGeneration;
  if (lastSnapshot && lastSnapshot.provision && lastSnapshot.provision.running) {
    provisionInFlight = true;
    $("provision-btn").disabled = true;
    msg.textContent = "Cancelling…";
    msg.classList.remove("error", "ok");
    try {
      const r = await apiFetch("/api/provision/cancel", { method: "POST" });
      if (!stillCurrent(generation)) return;
      const data = await r.json().catch(() => ({}));
      if (!r.ok || data.ok === false) {
        msg.textContent = formatApiDetail(data.message || data.detail, "Could not cancel.");
        msg.classList.add("error");
        $("provision-btn").disabled = false;
      } else {
        msg.textContent = "Cancel requested — waiting for current step to stop.";
      }
    } catch {
      if (!stillCurrent(generation)) return;
      msg.textContent = "Network error cancelling contribution.";
      msg.classList.add("error");
      $("provision-btn").disabled = false;
    } finally {
      // Always clear — Settings/reconnect bumps generation and must not leave the button stuck.
      provisionInFlight = false;
    }
    return;
  }
  const max_tb = parseFloat($("max_tb").value);
  const save_path = $("save_path").value || null;
  const preallocate = !!$("preallocate").checked;
  if (!(max_tb > 0)) {
    msg.textContent = "Enter a Content to add amount greater than 0.";
    msg.classList.add("error");
    return;
  }
  if (save_path) storedSet(SAVE_PATH_KEY, save_path);
  storedSet(PREALLOCATE_KEY, preallocate ? "1" : "0");
  provisionInFlight = true;
  $("provision-btn").disabled = true;
  msg.textContent = "Starting…";
  msg.classList.remove("error", "ok");
  try {
    const body = { max_tb, save_path, preallocate, allow_unknown_disk: false };
    let r = await apiFetch("/api/provision", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!stillCurrent(generation)) return;
    let data = await r.json().catch(() => ({}));
    if (data.code === "unknown_disk") {
      const ok = window.confirm(
        (data.message || "Free space is unknown for this destination.") +
          "\n\nContinue without a free-space check? The disk may fill."
      );
      if (!ok) {
        msg.textContent = "Cancelled — free space unknown.";
        msg.classList.add("error");
        $("provision-btn").disabled = false;
        return;
      }
      body.allow_unknown_disk = true;
      r = await apiFetch("/api/provision", {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (!stillCurrent(generation)) return;
      data = await r.json().catch(() => ({}));
    }
    if (!r.ok || data.ok === false) {
      msg.textContent = formatApiDetail(data.message || data.detail, "Could not start.");
      msg.classList.remove("ok");
      msg.classList.add("error");
      $("provision-btn").disabled = false;
    }
  } catch {
    if (!stillCurrent(generation)) return;
    msg.textContent = "Network error starting contribution.";
    msg.classList.add("error");
    $("provision-btn").disabled = false;
  } finally {
    provisionInFlight = false;
  }
});

// --- Global controls ---
function postControl(path, body, okMsg) {
  const run = async () => {
    const generation = connectGeneration;
    try {
      const r = await apiFetch(path, {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!stillCurrent(generation)) return;
      if (!r.ok) throw new Error(String(r.status));
      const data = await r.json().catch(() => ({}));
      if (!stillCurrent(generation)) return;
      if (data.controls) syncControlsFromSnapshot(data.controls);
      setControlsMsg(okMsg || "Updated", true);
    } catch {
      if (!stillCurrent(generation)) return;
      setControlsMsg("Control request failed.", false);
    }
  };
  controlQueue = controlQueue.then(run, run);
  return controlQueue;
}
$("pause-btn").addEventListener("click", () => postControl("/api/controls/pause", null, "Seeding paused"));
$("resume-btn").addEventListener("click", () => postControl("/api/controls/resume", null, "Seeding resumed"));
$("pause-dl-btn").addEventListener("click", () => postControl("/api/controls/pause-downloads", null, "Downloads paused"));
$("resume-dl-btn").addEventListener("click", () => postControl("/api/controls/resume-downloads", null, "Downloads resumed"));

function applyRateLimit(kind) {
  if (syncingControls) return;
  const sel = $(kind + "-limit").value;
  const custom = sel === "custom";
  $(kind + "-custom-wrap").hidden = !custom;
  let bytes_per_sec;
  if (sel === "-1") {
    bytes_per_sec = -1;
  } else if (custom) {
    const mbps = parseFloat($(kind + "-custom-mbps").value);
    if (!(mbps >= 0.1)) {
      setControlsMsg("Enter at least 0.1 Mbps.", false);
      return;
    }
    bytes_per_sec = Math.round(mbps * RATE_BPS_PER_MBPS);
  } else {
    bytes_per_sec = Math.round(parseFloat(sel) * RATE_BPS_PER_MBPS);
  }
  postControl(
    "/api/controls/" + kind + "-limit",
    { bytes_per_sec },
    (kind === "upload" ? "Upload" : "Download") + " limit updated"
  );
}

$("upload-limit").addEventListener("change", () => applyRateLimit("upload"));
$("upload-custom-mbps").addEventListener("change", () => {
  if ($("upload-limit").value === "custom") applyRateLimit("upload");
});
$("download-limit").addEventListener("change", () => applyRateLimit("download"));
$("download-custom-mbps").addEventListener("change", () => {
  if ($("download-limit").value === "custom") applyRateLimit("download");
});

$("torrent-filters").addEventListener("click", (e) => {
  const btn = e.target.closest(".filter-btn");
  if (!btn) return;
  const next = btn.dataset.filter || "all";
  torrentFilter = FILTER_KEYS.has(next) ? next : "all";
  storedSet(FILTER_KEY, torrentFilter);
  paintFilterButtons();
  if (lastSnapshot) renderTorrents(lastSnapshot.torrents || []);
});

// --- Space recovery ---
function renderSpacePreview(data) {
  const wrap = $("space-preview");
  wrap.hidden = false;
  wrap.replaceChildren();

  const summary = document.createElement("p");
  summary.className = "sub";
  summary.style.margin = "0";
  const freed = fmtBytes(data.freed_bytes);
  const over = Number(data.overshoot_bytes) || 0;
  summary.textContent =
    `Estimated to free about ${freed} by removing ${(data.selected || []).length} torrent(s).`;
  wrap.appendChild(summary);

  if (over > 0) {
    const w = document.createElement("div");
    w.className = "warn-box";
    const n = (data.selected || []).length;
    w.textContent =
      `Overshoot: this selection is estimated to free about ${fmtBytes(over)} more than requested` +
      (n >= 3 ? ` (${n} torrents — confirm the preview matches what you intend)` : "") +
      `. Confirm only if that is acceptable.`;
    wrap.appendChild(w);
  }

  if ((data.unscored || []).length) {
    const w = document.createElement("div");
    w.className = "warn-box";
    w.textContent =
      `Some torrents could not be ranked because seed count is unavailable (${data.unscored.length}). ` +
      `They were excluded from automatic selection — review them manually if needed.`;
    wrap.appendChild(w);
  }

  const ul = document.createElement("ul");
  ul.className = "space-list";
  for (const t of data.selected || []) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = t.name || t.infohash || "—";
    const meta = document.createElement("span");
    meta.className = "meta";
    const reclaim = t.reclaimable_bytes != null ? fmtBytes(t.reclaimable_bytes) + " reclaimable" : fmtBytes(t.size);
    meta.textContent = reclaim + (t.incomplete ? " · Incomplete" : "");
    li.append(name, meta);
    ul.appendChild(li);
  }
  if (!(data.selected || []).length) {
    const li = document.createElement("li");
    li.className = "sub";
    li.textContent = "No eligible torrents for that destination and amount.";
    ul.appendChild(li);
  }
  wrap.appendChild(ul);

  $("space-confirm-btn").disabled = !(data.selected || []).length;
}

$("space-preview-btn").addEventListener("click", async () => {
  const msg = $("space-msg");
  const gb = parseFloat($("space-gb").value);
  if (!(gb >= 1) || gb > 1000000) {
    msg.textContent = "Enter a GB amount between 1 and 1,000,000.";
    msg.classList.remove("ok");
    msg.classList.add("error");
    return;
  }
  const selectedPath = $("save_path").value;
  if (!selectedPath) {
    msg.textContent = "Choose a download destination under Contribute → Advanced first.";
    msg.classList.add("error");
    return;
  }
  msg.textContent = "Previewing…";
  msg.classList.remove("error");
  $("space-confirm-btn").disabled = true;
  spacePreview = null;
  const requestId = ++spaceRequestId;
  const generation = connectGeneration;
  try {
    const body = { gb, save_path: selectedPath };
    const r = await apiFetch("/api/space/preview", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!stillCurrent(generation)) return;
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(formatApiDetail(err.detail, "preview failed"));
    }
    const nextPreview = await r.json();
    if (requestId !== spaceRequestId || $("save_path").value !== selectedPath) return;
    spacePreview = nextPreview;
    renderSpacePreview(spacePreview);
    msg.textContent = "Review the list, then confirm to permanently remove files.";
  } catch (e) {
    if (requestId !== spaceRequestId || !stillCurrent(generation)) return;
    msg.textContent = e.message || "Preview failed.";
    msg.classList.add("error");
    $("space-preview").hidden = true;
  }
});

$("space-confirm-btn").addEventListener("click", async () => {
  if (!spacePreview || !(spacePreview.selected || []).length) return;
  const msg = $("space-msg");
  const preview = spacePreview;
  const freeId = ++spaceFreeId;
  const generation = connectGeneration;
  const hashes = preview.selected.map((t) => t.infohash).filter(Boolean);
  const freedBytes = preview.freed_bytes;
  msg.textContent = "Removing…";
  msg.classList.remove("error");
  $("space-confirm-btn").disabled = true;
  try {
    const body = {
      infohashes: hashes,
      confirm: true,
      token: preview.token,
      request_bytes: preview.request_bytes,
    };
    if (preview.save_path) body.save_path = preview.save_path;
    else {
      const save_path = $("save_path").value;
      if (save_path) body.save_path = save_path;
    }
    const r = await apiFetch("/api/space/free", {
      method: "POST",
      body: JSON.stringify(body),
      timeoutMs: 120000,
    });
    if (!stillCurrent(generation) || freeId !== spaceFreeId) {
      // Own attempt interrupted (reconnect/Settings) — don't leave "Removing…".
      if (freeId === spaceFreeId && spacePreview === preview) {
        msg.textContent = "Interrupted — run Preview again if needed.";
        $("space-confirm-btn").disabled = !(preview.selected || []).length;
      }
      return;
    }
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      if (r.status === 400 || r.status === 409) {
        if (spacePreview === preview) clearSpacePreview();
      }
      throw new Error(formatApiDetail(err.detail, "remove failed"));
    }
    const data = await r.json();
    if (!stillCurrent(generation) || freeId !== spaceFreeId) {
      if (freeId === spaceFreeId && spacePreview === preview) {
        msg.textContent = "Interrupted — run Preview again if needed.";
        $("space-confirm-btn").disabled = !(preview.selected || []).length;
      }
      return;
    }
    if (spacePreview === preview) clearSpacePreview();
    const removed = Number(data.removed) || 0;
    if (!removed) {
      msg.textContent = "No torrents were removed.";
      msg.classList.add("error");
      return;
    }
    if (data.files_deleted === false) {
      msg.textContent =
        `Removed ${removed} torrent(s) from the client, but content files were not deleted.`;
      return;
    }
    if (data.files_deleted == null) {
      msg.textContent =
        `Removed ${removed} torrent(s). File deletion was not verified — confirm free space in your torrent client.`;
      return;
    }
    msg.textContent =
      `Freed about ${fmtBytes(freedBytes)} by removing ${removed} torrent(s).`;
  } catch (e) {
    if (!stillCurrent(generation) || freeId !== spaceFreeId) {
      if (freeId === spaceFreeId && spacePreview === preview) {
        msg.textContent = "Interrupted — run Preview again if needed.";
        $("space-confirm-btn").disabled = !(preview.selected || []).length;
      }
      return;
    }
    msg.textContent = e.message || "Remove failed.";
    msg.classList.add("error");
    if (spacePreview === preview) {
      // Keep confirm disabled after failure until a new Preview.
      $("space-confirm-btn").disabled = true;
    }
  }
});

$("space-gb").addEventListener("input", clearSpacePreview);

// --- Sort ---
$("torrent-head").addEventListener("click", (e) => {
  const btn = e.target.closest(".sort-btn");
  if (!btn) return;
  const key = btn.dataset.key;
  if (!SORT_KEYS.has(key)) return;
  if (sort.key === key) sort.dir = sort.dir === "asc" ? "desc" : "asc";
  else { sort.key = key; sort.dir = key === "name" || key === "state" ? "asc" : "desc"; }
  storedSet(SORT_KEY, JSON.stringify(sort));
  paintSortHeaders();
  if (lastSnapshot) renderTorrents(lastSnapshot.torrents);
});

// --- Modal helpers (focus trap) ---
function focusable(root) {
  return [...root.querySelectorAll(
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )].filter((el) => el.offsetParent !== null || el === document.activeElement);
}

function trapFocus(e, root) {
  if (e.key !== "Tab") return;
  const nodes = focusable(root);
  if (!nodes.length) return;
  const first = nodes[0], last = nodes[nodes.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function openModal(backdropId, panelId, focusId) {
  const backdrop = $(backdropId);
  backdrop.hidden = false;
  backdrop.classList.add("open");
  const focusEl = focusId ? $(focusId) : focusable($(panelId))[0];
  if (focusEl) focusEl.focus();
}

function closeModal(backdropId, restoreEl) {
  const backdrop = $(backdropId);
  backdrop.classList.remove("open");
  backdrop.hidden = true;
  if (restoreEl) restoreEl.focus();
}

// --- Settings ---
function isQbit(backend) {
  const b = String(backend != null ? backend : appConfig.backend || "").toLowerCase();
  return ["qbittorrent", "qbit", "qb"].includes(b);
}

function syncSettingsQbitFields() {
  const q = isQbit($("settings-backend").value);
  $("settings-qbit").hidden = !q;
  const hint = $("settings-web-port-hint");
  if (hint) hint.hidden = !q;
}

function paintBackendLabel() {
  const be = $("backend-label");
  if (!be) return;
  const name = isQbit() ? "qBittorrent" : "Embedded libtorrent (default)";
  be.replaceChildren();
  be.append("Backend: " + name + " — change in Settings ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", "14");
  svg.setAttribute("height", "14");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute(
    "d",
    "M19.14 12.94c.04-.31.06-.63.06-.94s-.02-.63-.06-.94l2.03-1.58a.5.5 0 0 0 .12-.61l-1.92-3.32a.5.5 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.5.5 0 0 0-.48-.41h-3.84a.5.5 0 0 0-.48.41l-.36 2.54c-.59.24-1.13.56-1.62.94l-2.39-.96a.5.5 0 0 0-.59.22L2.74 8.87a.5.5 0 0 0 .12.61l2.03 1.58c-.04.31-.06.63-.06.94s.02.63.06.94l-2.03 1.58a.5.5 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.48-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32a.5.5 0 0 0-.12-.61l-2.03-1.58zM12 15.6A3.6 3.6 0 1 1 12 8.4a3.6 3.6 0 0 1 0 7.2z"
  );
  svg.appendChild(path);
  be.append(svg);
}

function openSettings() {
  settingsFocusBefore = document.activeElement;
  const backend = isQbit(appConfig.backend) ? "qbittorrent" : "libtorrent";
  $("settings-backend").value = backend;
  $("settings-qbit-url").value = appConfig.qbit_url || (appConfig.defaults && appConfig.defaults.qbit_url) || "http://127.0.0.1:8080";
  $("settings-qbit-user").value = appConfig.qbit_user || (appConfig.defaults && appConfig.defaults.qbit_user) || "admin";
  $("settings-qbit-pass").value = "";
  const clearPass = $("settings-qbit-pass-clear");
  if (clearPass) clearPass.checked = false;
  $("settings-qbit-pass").placeholder = appConfig.qbit_pass_set
    ? "Leave blank to keep current"
    : "Optional if localhost bypass is enabled";
  $("settings-category").value =
    appConfig.qbit_category || (appConfig.defaults && appConfig.defaults.qbit_category) || "";
  const authWrap = $("settings-auth");
  if (authWrap) {
    authWrap.hidden = !appConfig.auth_required;
    $("settings-api-token").value = storedGet(API_TOKEN_KEY) || "";
  }
  $("settings-msg").textContent = "";
  $("settings-msg").classList.remove("error");
  syncSettingsQbitFields();
  openModal("settings-modal", "settings-panel", "settings-backend");
}

function closeSettings() {
  closeModal("settings-modal", settingsFocusBefore);
  settingsFocusBefore = null;
}

$("settings-btn").addEventListener("click", openSettings);
$("settings-cancel").addEventListener("click", closeSettings);
$("settings-backend").addEventListener("change", syncSettingsQbitFields);
$("settings-modal").addEventListener("click", (e) => {
  if (e.target === $("settings-modal")) closeSettings();
});
$("settings-modal").addEventListener("keydown", (e) => trapFocus(e, $("settings-panel")));

$("settings-save").addEventListener("click", async () => {
  const msg = $("settings-msg");
  const torrent_backend = $("settings-backend").value;
  const knownBackend = isQbit(appConfig.backend) ? "qbittorrent" : "libtorrent";
  // Only send torrent_backend when the user actually changed it — a token-only
  // save must not default-switch away from the live backend.
  const body = {};
  if (torrent_backend !== knownBackend) body.torrent_backend = torrent_backend;
  const btn = $("settings-save");
  const tokenEl = $("settings-api-token");
  const nextToken = tokenEl && appConfig.auth_required ? tokenEl.value.trim() : null;
  if (appConfig.auth_required && !nextToken) {
    msg.textContent = "API token cannot be empty while authentication is enabled.";
    msg.classList.add("error");
    return;
  }
  // Token-only first: after 401 the form may still show empty/default qBit
  // placeholders — do not require category/url validation to save the token.
  // When config loaded successfully, only skip the PUT if qBit fields match appConfig.
  if (nextToken && torrent_backend === knownBackend) {
    const passDirty =
      !!$("settings-qbit-pass").value ||
      ($("settings-qbit-pass-clear") && $("settings-qbit-pass-clear").checked);
    let qbitDirty = false;
    if (!authBlocked && isQbit(torrent_backend)) {
      qbitDirty =
        $("settings-category").value.trim() !== String(appConfig.qbit_category || "").trim() ||
        $("settings-qbit-url").value.trim() !== String(appConfig.qbit_url || "").trim() ||
        $("settings-qbit-user").value.trim() !== String(appConfig.qbit_user || "").trim();
    }
    if (!passDirty && (authBlocked || !qbitDirty)) {
      storedSet(API_TOKEN_KEY, nextToken);
      authBlocked = false;
      closeSettings();
      toast("API token saved");
      invalidateConnection();
      try {
        const cfg = await apiFetch("/api/config").then((r) => {
          if (!r.ok) throw new Error(String(r.status));
          return r.json();
        });
        appConfig = { ...appConfig, ...cfg };
        paintBackendLabel();
      } catch { /* connect() will surface auth issues */ }
      loadStorageOptions();
      connect();
      return;
    }
  }
  if (isQbit(torrent_backend)) {
    const qbit_category = $("settings-category").value.trim();
    const qbit_url = $("settings-qbit-url").value.trim();
    const qbit_user = $("settings-qbit-user").value.trim();
    const qbit_pass = $("settings-qbit-pass").value;
    if (!qbit_category) {
      msg.textContent = "Category name cannot be empty.";
      msg.classList.add("error");
      return;
    }
    if (!qbit_url) {
      msg.textContent = "qBittorrent URL cannot be empty.";
      msg.classList.add("error");
      return;
    }
    if (!qbit_user) {
      msg.textContent = "qBittorrent username cannot be empty.";
      msg.classList.add("error");
      return;
    }
    body.qbit_category = qbit_category;
    body.qbit_url = qbit_url;
    body.qbit_user = qbit_user;
    const clearPass = $("settings-qbit-pass-clear");
    if (clearPass && clearPass.checked) body.qbit_pass = "";
    else if (qbit_pass !== "") body.qbit_pass = qbit_pass;
  }
  if (!Object.keys(body).length && nextToken) {
    storedSet(API_TOKEN_KEY, nextToken);
    authBlocked = false;
    closeSettings();
    toast("API token saved");
    invalidateConnection();
    loadStorageOptions();
    connect();
    return;
  }
  if (!Object.keys(body).length) {
    msg.textContent = "No settings changed.";
    msg.classList.add("error");
    return;
  }
  btn.disabled = true;
  msg.textContent = "Saving…";
  msg.classList.remove("error");
  try {
    const r = await apiFetch("/api/settings", {
      method: "PUT",
      body: JSON.stringify(body),
      headers: nextToken ? { "X-API-Token": nextToken } : undefined,
      timeoutMs: 120000,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(formatApiDetail(data.detail, "Could not save settings"));
    if (nextToken != null) storedSet(API_TOKEN_KEY, nextToken);
    authBlocked = false;
    appConfig = { ...appConfig, ...data, backend: data.backend || torrent_backend };
    if (data.qbit_category) appConfig.qbit_category = data.qbit_category;
    if (data.qbit_url) appConfig.qbit_url = data.qbit_url;
    if (data.qbit_user) appConfig.qbit_user = data.qbit_user;
    if ("qbit_pass_set" in data) appConfig.qbit_pass_set = data.qbit_pass_set;
    paintBackendLabel();
    closeSettings();
    toast(
      data.rebuilt
        ? "Backend updated — existing torrents stay in the previous client until re-added"
        : "Settings saved"
    );
    invalidateConnection();
    clearSpacePreview();
    loadStorageOptions();
    connect();
  } catch (err) {
    msg.textContent = err.message || "Could not save settings.";
    msg.classList.add("error");
  } finally {
    btn.disabled = false;
  }
});

// --- Sharing ---
function shareLanding() {
  return publicBase ? publicBase + "/view" : "";
}

const ANNA_HOME = "https://annas-archive.pk/";

function shareContent() {
  const c = latestCoverage;
  // Share only indexed contribution — never fall back to unverified local bytes.
  const covBytes = c && c.index_ready && Number(c.seeded_bytes) > 0 ? Number(c.seeded_bytes) : 0;
  const { size, pct } = roundImpact(covBytes, covBytes > 0 ? c.percent : null);
  const title = "Help preserve Anna's Archive";
  let text =
    "📚 Anna's Archive is the largest open library in human history — books, papers, " +
    "and knowledge kept free from censorship, paywalls, and link rot. It survives only " +
    "because volunteers seed its torrents. I'm helping by seeding " + size;
  if (pct) text += ` (${pct} of the whole library)`;
  text += ". Join me and keep human knowledge free for everyone";
  const url = shareLanding();
  return { title, text, url };
}

/** Clipboard text: impact + Anna's Archive home only (not /view). */
function copyShareText() {
  return shareContent().text + ": " + ANNA_HOME;
}

function shareBlob() {
  const c = shareContent();
  return c.url ? c.text + ".\n\n" + c.url : c.text + ": " + ANNA_HOME;
}

const enc = encodeURIComponent;
const SHARE_URLS = {
  x: ({ text, url }) =>
    `https://twitter.com/intent/tweet?text=${enc(text)}${url ? "&url=" + enc(url) : ""}`,
  bluesky: ({ text, url }) =>
    `https://bsky.app/intent/compose?text=${enc(url ? text + " " + url : text)}`,
  reddit: ({ title, url }) =>
    url
      ? `https://www.reddit.com/submit?url=${enc(url)}&title=${enc(title)}`
      : `https://www.reddit.com/submit?title=${enc(title)}&text=${enc(shareBlob())}`,
  telegram: ({ text, url }) =>
    `https://t.me/share/url?${url ? "url=" + enc(url) + "&" : ""}text=${enc(text)}`,
  whatsapp: () => `https://wa.me/?text=${enc(shareBlob())}`,
  facebook: ({ url }) =>
    url
      ? `https://www.facebook.com/sharer/sharer.php?u=${enc(url)}`
      : null,
  linkedin: ({ url }) =>
    url
      ? `https://www.linkedin.com/sharing/share-offsite/?url=${enc(url)}`
      : null,
  email: ({ title }) =>
    `mailto:?subject=${enc(title)}&body=${enc(shareBlob())}`,
};

function toast(msg) {
  const t = $("share-toast");
  if (!t) return;
  t.textContent = msg;
  setTimeout(() => { if (t.textContent === msg) t.textContent = ""; }, 2500);
}

function openShareModal(net) {
  shareFocusBefore = document.activeElement;
  pendingShare = net;
  const c = shareContent();
  $("share-modal-body").textContent = net === "copy" ? copyShareText() : shareBlob();
  $("share-modal-err").textContent = "";
  $("share-modal-note").textContent = net === "copy"
    ? "Copied text ends with the Anna's Archive site link only."
    : publicBase
    ? "This text and the public /view link will be shared. No paths or hashes are included."
    : "This text will be shared. No public view link (PUBLIC_URL not set).";
  const confirm = $("share-modal-confirm");
  confirm.textContent = net === "copy" ? "Copy" : net === "native" ? "Share" : "Open";
  const mastodonField = $("mastodon-field");
  mastodonField.hidden = net !== "mastodon";
  if (net === "mastodon") {
    $("mastodon-instance").value = storedGet(MASTODON_KEY) || "mastodon.social";
  }
  if ((net === "facebook" || net === "linkedin") && !c.url) {
    $("share-modal-note").textContent =
      "This network needs a public URL. Set PUBLIC_URL on the server, or use Copy instead.";
    confirm.disabled = true;
  } else {
    confirm.disabled = false;
  }
  openModal("share-modal", "share-modal-panel",
    net === "mastodon" ? "mastodon-instance" : "share-modal-confirm");
}

function closeShareModal() {
  closeModal("share-modal", shareFocusBefore);
  pendingShare = null;
  shareFocusBefore = null;
}

async function executeShare(net) {
  if (!hasVerifiedContribution(lastSnapshot)) {
    toast("Share unlocks after a verified indexed contribution.");
    updateShareGate(lastSnapshot);
    return;
  }
  const c = shareContent();
  if (!(Number(latestCoverage?.seeded_bytes) > 0)) {
    toast("Nothing indexed to share right now.");
    return;
  }
  if (net === "native") {
    try {
      const payload = { title: c.title, text: c.text };
      if (c.url) payload.url = c.url;
      await navigator.share(payload);
    } catch { /* cancelled */ }
    return;
  }
  if (net === "copy") {
    try {
      await navigator.clipboard.writeText(copyShareText());
      toast("Copied to clipboard");
    } catch {
      toast("Copy failed — select the text manually.");
    }
    return;
  }
  if (net === "mastodon") {
    const inst = (storedGet(MASTODON_KEY) || "mastodon.social")
      .replace(/^https?:\/\//, "").replace(/\/+$/, "");
    window.open(`https://${inst}/share?text=${enc(shareBlob())}`, "_blank", "noopener");
    return;
  }
  const builder = SHARE_URLS[net];
  if (!builder) return;
  const href = builder(c);
  if (!href) {
    toast("This network needs PUBLIC_URL.");
    return;
  }
  window.open(href, "_blank", "noopener");
}

$("share-modal-cancel").addEventListener("click", closeShareModal);
$("share-modal").addEventListener("click", (e) => {
  if (e.target === $("share-modal")) closeShareModal();
});
$("share-modal").addEventListener("keydown", (e) => trapFocus(e, $("share-modal-panel")));
$("share-modal-confirm").addEventListener("click", async () => {
  const net = pendingShare;
  if (net === "mastodon") {
    let inst = ($("mastodon-instance").value || "").trim()
      .replace(/^https?:\/\//, "").replace(/\/+$/, "");
    if (!inst) {
      $("share-modal-err").textContent = "Enter a Mastodon instance.";
      $("share-modal-err").classList.add("error");
      return;
    }
    storedSet(MASTODON_KEY, inst);
  }
  closeShareModal();
  if (net) await executeShare(net);
});

function openRemoveModal(t) {
  if (!t || !t.infohash) return;
  removeFocusBefore = document.activeElement;
  pendingRemove = {
    infohash: String(t.infohash).toLowerCase(),
    name: t.name || t.infohash.slice(0, 12),
    size: t.size || 0,
  };
  $("remove-modal-name").textContent = pendingRemove.name;
  $("remove-modal-meta").textContent = fmtBytes(pendingRemove.size) + " · files will be deleted";
  $("remove-modal-err").textContent = "";
  $("remove-modal-err").classList.remove("error");
  $("remove-modal-confirm").disabled = false;
  openModal("remove-modal", "remove-modal-panel", "remove-modal-confirm");
}

function closeRemoveModal() {
  closeModal("remove-modal", removeFocusBefore);
  pendingRemove = null;
  removeFocusBefore = null;
}

$("remove-modal-cancel").addEventListener("click", closeRemoveModal);
$("remove-modal").addEventListener("click", (e) => {
  if (e.target === $("remove-modal")) closeRemoveModal();
});
$("remove-modal").addEventListener("keydown", (e) => trapFocus(e, $("remove-modal-panel")));
$("remove-modal-confirm").addEventListener("click", async () => {
  const item = pendingRemove;
  if (!item) return;
  const btn = $("remove-modal-confirm");
  const generation = connectGeneration;
  btn.disabled = true;
  $("remove-modal-err").textContent = "Removing…";
  $("remove-modal-err").classList.remove("error");
  try {
    const r = await apiFetch("/api/torrents/remove", {
      method: "POST",
      body: JSON.stringify({
        infohash: item.infohash,
        confirm: true,
        delete_files: true,
      }),
      timeoutMs: 120000,
    });
    if (!stillCurrent(generation)) {
      // Reconnect/Settings mid-remove — don't leave modal stuck on "Removing…".
      if (pendingRemove === item) {
        $("remove-modal-err").textContent = "Interrupted — try again.";
        $("remove-modal-err").classList.add("error");
        btn.disabled = false;
      }
      return;
    }
    const data = await r.json().catch(() => ({}));
    if (r.status === 404) {
      closeRemoveModal();
      toast("Torrent already gone");
      if (lastSnapshot && Array.isArray(lastSnapshot.torrents)) {
        lastSnapshot.torrents = lastSnapshot.torrents.filter(
          (t) => (t.infohash || "").toLowerCase() !== item.infohash
        );
        renderTorrents(lastSnapshot.torrents);
      }
      return;
    }
    if (!r.ok) {
      // 409 = session remove may have partially applied; refresh and close.
      if (r.status === 409) {
        closeRemoveModal();
        toast(formatApiDetail(data.detail, "Removal incomplete — refresh status"));
        return;
      }
      throw new Error(formatApiDetail(data.detail, "Remove failed"));
    }
    closeRemoveModal();
    if (data.files_deleted === false) {
      toast("Torrent removed; files need attention");
    } else if (data.files_deleted == null && data.removed) {
      toast("Torrent removed; confirm file deletion in your client if needed");
    } else {
      toast("Torrent removed");
    }
    if (data.removed && lastSnapshot && Array.isArray(lastSnapshot.torrents)) {
      lastSnapshot.torrents = lastSnapshot.torrents.filter(
        (t) => (t.infohash || "").toLowerCase() !== item.infohash
      );
      renderTorrents(lastSnapshot.torrents);
    }
  } catch (err) {
    if (!stillCurrent(generation)) {
      if (pendingRemove === item) {
        $("remove-modal-err").textContent = "Interrupted — try again.";
        $("remove-modal-err").classList.add("error");
        btn.disabled = false;
      }
      return;
    }
    $("remove-modal-err").textContent = err.message || "Could not remove torrent";
    $("remove-modal-err").classList.add("error");
    btn.disabled = false;
  }
});

document.querySelector("#share-active .share-row").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-net]");
  if (!btn) return;
  openShareModal(btn.dataset.net);
});

if (navigator.share) {
  const n = document.querySelector(".share-btn.native");
  if (n) n.hidden = false;
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if ($("remove-modal").classList.contains("open")) closeRemoveModal();
  else if ($("share-modal").classList.contains("open")) closeShareModal();
  else if ($("settings-modal").classList.contains("open")) closeSettings();
});

// --- View mode ---
if (VIEW_MODE) {
  document.body.classList.add("view-mode");
  const cta = $("view-cta");
  if (cta) cta.hidden = false;
}

// --- Config + SSE ---
paintFilterButtons();

async function bootstrapConfig() {
  try {
    const pub = await fetchWithTimeout("/api/public/config").then((r) => r.json());
    appConfig = { ...appConfig, ...pub };
    if (pub.public_url) publicBase = String(pub.public_url).replace(/\/+$/, "");
    if (pub.auth_required && !pub.auth_configured && !VIEW_MODE) {
      toast("Set API_TOKEN on the server before using controls");
    } else if (pub.auth_required && !VIEW_MODE && !storedGet(API_TOKEN_KEY)) {
      toast("API token required — open Settings");
    }
  } catch { /* ignore */ }
  if (VIEW_MODE) return;
  try {
    const cfg = await apiFetch("/api/config").then((r) => {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    });
    appConfig = { ...appConfig, ...cfg };
    if (cfg.public_url) publicBase = String(cfg.public_url).replace(/\/+$/, "");
    paintBackendLabel();
    if (lastSnapshot) updateShareGate(lastSnapshot);
  } catch (err) {
    console.error("config failed", err);
  }
}

function scheduleReconnect(generation) {
  if (authBlocked || reconnectTimer || generation !== connectGeneration) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (generation === connectGeneration && !authBlocked) connect();
  }, 3000);
}

async function connect() {
  const generation = ++connectGeneration;
  setConnPill("reconnecting", "Connecting…");
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (liveEs) {
    try { liveEs.close(); } catch { /* ignore */ }
    liveEs = null;
  }
  if (statusAbort) {
    try { statusAbort.abort(); } catch { /* ignore */ }
  }
  const abort = new AbortController();
  statusAbort = abort;
  const statusPath = VIEW_MODE ? "/api/public/status" : "/api/status";
  const eventsPath = VIEW_MODE ? "/api/public/events" : "/api/events";
  try {
    let ticket = null;
    if (!VIEW_MODE) {
      const ticketResponse = await apiFetch("/api/events/ticket", { signal: abort.signal });
      if (generation !== connectGeneration || authBlocked) return;
      if (!ticketResponse.ok) throw new Error(String(ticketResponse.status));
      ticket = (await ticketResponse.json()).ticket || null;
    }
    const r = await apiFetch(statusPath, { signal: abort.signal });
    if (generation !== connectGeneration || authBlocked) return;
    if (!r.ok) throw new Error(String(r.status));
    const snapshot = await r.json();
    if (generation !== connectGeneration || authBlocked) return;
    render(snapshot);
    const es = new EventSource(eventsUrl(eventsPath, ticket));
    liveEs = es;
    es.onmessage = (e) => {
      if (es !== liveEs || generation !== connectGeneration) return;
      try {
        render(JSON.parse(e.data));
      } catch (err) {
        console.error("SSE frame failed", err);
      }
    };
    es.onerror = () => {
      if (es !== liveEs || generation !== connectGeneration) return;
      es.close();
      if (liveEs === es) liveEs = null;
      setConnPill("reconnecting", "Reconnecting…");
      // Stale metrics stay visible but share unlock must not rely on them.
      if (!VIEW_MODE) {
        $("share-gate").hidden = false;
        $("share-active").hidden = true;
        $("provision-btn").disabled = true;
        resetControlsChrome();
      }
      scheduleReconnect(generation);
    };
  } catch (err) {
    // Intentional abort from invalidateConnection bumps generation / clears statusAbort.
    // Timeout also uses AbortError but keeps generation — must reconnect.
    if (generation !== connectGeneration || authBlocked) return;
    if (err && err.name === "AbortError" && statusAbort !== abort) return;
    console.error("status bootstrap failed", err);
    setConnPill("reconnecting", "Reconnecting…");
    if (!VIEW_MODE) {
      $("share-gate").hidden = false;
      $("share-active").hidden = true;
      $("provision-btn").disabled = true;
      resetControlsChrome();
    }
    scheduleReconnect(generation);
  }
}

// Placeholder first — must not run after fetch/render or it overwrites the table.
$("torrent-rows").replaceChildren();
$("torrent-rows").appendChild(emptyRow(VIEW_MODE ? 8 : 9, "Connecting…"));
bootstrapConfig().then(() => {
  if (!VIEW_MODE) loadStorageOptions();
  connect();
});
})();
