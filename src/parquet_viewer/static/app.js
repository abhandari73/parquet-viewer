/* Parquet Viewer front end - plain JavaScript, no external libraries. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const NUMERIC = new Set(["int", "float", "decimal"]);

const OPS = {
  "=": "=", "!=": "≠", "<": "<", "<=": "≤", ">": ">", ">=": "≥", between: "between",
  in: "in", not_in: "not in", contains: "contains", not_contains: "doesn't contain",
  starts: "starts with", ends: "ends with", regex: "matches regex",
  null: "is null", not_null: "is not null", nan: "is NaN",
};
const NO_VALUE = new Set(["null", "not_null", "nan"]);

function opsFor(kind) {
  const cmp = ["=", "!=", "<", "<=", ">", ">=", "between", "in", "not_in"];
  const txt = ["contains", "not_contains", "starts", "ends", "regex"];
  if (kind === "string") return ["contains", "=", "!=", "in", "not_in", "starts", "ends", "not_contains", "regex", "<", ">", "null", "not_null"];
  if (kind === "float") return [...cmp, "null", "not_null", "nan"];
  if (NUMERIC.has(kind) || kind === "date" || kind === "timestamp" || kind === "time") return [...cmp, "null", "not_null", ...txt.slice(0, 1)];
  if (kind === "bool") return ["=", "!=", "null", "not_null"];
  return ["contains", "not_contains", "null", "not_null", "="];
}

const S = {
  cfg: null,
  dir: "",
  listing: null,
  table: null,          // {path, dataset, info}
  view: null,           // {columns, filters, where, search, sort, offset, limit}
  result: null,
  tab: "data",
  selRow: -1,
};

function freshView(limit) {
  return { columns: null, filters: [], where: "", search: "", sort: [], offset: 0, limit: limit || +$("#page-size").value || 100 };
}

/* ---------------------------------------------------------------- API */
const controllers = {};
let busy = 0;
function setBusy(d) {
  busy = Math.max(0, busy + d);
  const p = $("#progress");
  if (busy) { p.classList.add("on"); } else { p.classList.remove("on"); p.style.width = "100%"; setTimeout(() => (p.style.width = ""), 250); }
}

async function api(name, { params, body, channel } = {}) {
  if (channel && controllers[channel]) controllers[channel].abort();
  const ctl = new AbortController();
  if (channel) controllers[channel] = ctl;
  const qs = params ? "?" + new URLSearchParams(params) : "";
  setBusy(1);
  try {
    const res = await fetch("/api/" + name + qs, {
      method: body ? "POST" : "GET",
      headers: { "X-PQV": "1", "Content-Type": "application/json" },
      body: body ? JSON.stringify(channel ? { ...body, channel } : body) : undefined,
      signal: ctl.signal,
    });
    const data = await res.json().catch(() => ({ error: "Bad response from server (" + res.status + ")" }));
    if (!res.ok || data.error) throw new Error(data.error || res.statusText);
    return data;
  } finally {
    setBusy(-1);
    if (channel && controllers[channel] === ctl) delete controllers[channel];
  }
}
const isAbort = (e) => e && (e.name === "AbortError" || /cancelled by a newer/.test(e.message));

function toast(msg, err = false) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), err ? 9000 : 3500);
}

/* ---------------------------------------------------------------- formatting */
const nf = new Intl.NumberFormat();
const fmtCount = (n) => (n == null ? "–" : nf.format(n));
function fmtBytes(b) {
  if (b == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i ? b.toFixed(b < 10 ? 1 : 0) : b) + " " + u[i];
}
function fmtTime(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
function fmtNum(v) {
  if (typeof v !== "number") return String(v);
  if (Number.isInteger(v)) return String(v);
  const a = Math.abs(v);
  if (a >= 1e15) return v.toExponential(4);
  if (a !== 0 && a < 1e-3) return v.toPrecision(4);
  return String(parseFloat(v.toFixed(6)));
}
function fmtStat(v) {
  if (v == null) return "–";
  if (typeof v === "number") return Number.isInteger(v) ? nf.format(v) : fmtNum(v);
  return String(v);
}
function cellHTML(v, kind) {
  if (v === null || v === undefined) return ['<td class="nul">null</td>', ""];
  if (NUMERIC.has(kind)) {
    if (v === "NaN" || v === "Infinity" || v === "-Infinity") return [`<td class="num nan">${v}</td>`, ""];
    return [`<td class="num">${esc(fmtNum(v))}</td>`, ""];
  }
  if (kind === "bool") return [`<td class="${v ? "bool-t" : ""}">${v}</td>`, ""];
  let s = typeof v === "object" ? JSON.stringify(v) : String(v);
  if (kind === "timestamp" && s.endsWith(" 00:00:00")) s = s.slice(0, -9);
  const title = s.length > 40 ? ` title="${esc(s.slice(0, 2000))}"` : "";
  return [`<td${title}>${esc(s.length > 500 ? s.slice(0, 500) + "…" : s)}</td>`, ""];
}
function plainValue(v) {
  if (v === null || v === undefined) return null;
  return typeof v === "object" ? JSON.stringify(v, null, 1) : String(v);
}

/* ---------------------------------------------------------------- URL state */
function saveHash() {
  const st = { d: S.dir };
  if (S.table) {
    st.p = S.table.path; st.ds = S.table.dataset ? 1 : 0; st.t = S.tab;
    const v = S.view;
    if (v.columns) st.c = v.columns;
    if (v.filters.length) st.f = v.filters;
    if (v.where) st.w = v.where;
    if (v.search) st.s = v.search;
    if (v.sort.length) st.o = v.sort;
    if (v.offset) st.of = v.offset;
    if (v.limit !== 100) st.l = v.limit;
  }
  const h = "#" + encodeURIComponent(JSON.stringify(st));
  if (location.hash !== h) history.replaceState(null, "", h);
}
function readHash() {
  try { return JSON.parse(decodeURIComponent(location.hash.slice(1))) || {}; } catch { return {}; }
}

/* ---------------------------------------------------------------- folder browser */
async function loadDir(path, quiet) {
  try {
    const data = await api("list", { params: { path }, channel: "list" });
    S.dir = data.path; S.listing = data;
    renderDir();
    saveHash();
  } catch (e) {
    if (isAbort(e)) return;
    if (!quiet) toast(e.message, true);
    if (path) loadDir("", true);
  }
}

function renderDir() {
  const L = S.listing;
  const parts = L.path ? L.path.split("/") : [];
  const crumbs = [`<a data-p="">${esc(S.cfg.root_name)}</a>`];
  parts.forEach((p, i) => crumbs.push(`<span class="sep">/</span><a data-p="${esc(parts.slice(0, i + 1).join("/"))}">${esc(p)}</a>`));
  $("#crumbs").innerHTML = crumbs.join("");
  $$("#crumbs a").forEach((a) => (a.onclick = () => loadDir(a.dataset.p)));

  const f = $("#dir-filter").value.trim().toLowerCase();
  const items = L.entries.filter((e) => !f || e.name.toLowerCase().includes(f));
  const rows = [];
  if (L.parent !== null && !f) rows.push(`<li class="dir" data-up="1"><span class="ic">↰</span><span class="nm">..</span></li>`);
  for (const e of items) {
    const p = (L.path ? L.path + "/" : "") + e.name;
    if (e.type === "dir") {
      const meta = e.n_parquet ? `${fmtCount(e.n_parquet)} file${e.n_parquet === 1 ? "" : "s"}` : e.n_dirs ? `${fmtCount(e.n_dirs)} folder${e.n_dirs === 1 ? "" : "s"}` : "";
      rows.push(`<li class="dir" data-dir="${esc(p)}" title="${esc(e.name)}"><span class="ic">▸</span><span class="nm">${esc(e.name)}</span><span class="mt">${meta}</span></li>`);
    } else {
      const cur = S.table && !S.table.dataset && S.table.path === p ? " current" : "";
      rows.push(`<li class="file${cur}" data-file="${esc(p)}" title="${esc(e.name)} · ${fmtTime(e.mtime)}"><span class="ic">▦</span><span class="nm">${esc(e.name)}</span><span class="mt">${fmtBytes(e.size)}</span></li>`);
    }
  }
  if (!items.length) rows.push(`<li class="none">${f ? "No match" : "No parquet files or folders here"}</li>`);
  $("#entries").innerHTML = rows.join("");
  $$("#entries li").forEach((li) => {
    if (li.dataset.up) li.onclick = () => loadDir(L.parent);
    else if (li.dataset.dir !== undefined) li.onclick = () => loadDir(li.dataset.dir);
    else if (li.dataset.file !== undefined) li.onclick = () => openTable(li.dataset.file, false);
  });

  const nFiles = L.entries.filter((e) => e.type === "file").length;
  const ob = $("#open-folder");
  ob.hidden = !L.can_open_as_table;
  ob.textContent = `⧉ Open all files in “${L.name}” as one table` + (nFiles ? ` (${fmtCount(nFiles)} here + subfolders)` : " (incl. subfolders)");
  ob.onclick = () => openTable(L.path, true);
  const nd = L.entries.length - nFiles;
  $("#dir-foot").textContent = `${fmtCount(nd)} folder${nd === 1 ? "" : "s"}, ${fmtCount(nFiles)} parquet file${nFiles === 1 ? "" : "s"}` + (L.other_files ? `, ${fmtCount(L.other_files)} other file${L.other_files === 1 ? "" : "s"} not shown` : "");
}

/* ---------------------------------------------------------------- recent */
function recentList() { try { return JSON.parse(localStorage.getItem("pqv-recent") || "[]"); } catch { return []; } }
function pushRecent(path, dataset) {
  const key = S.cfg.root;
  let r = recentList().filter((x) => !(x.r === key && x.p === path && !!x.d === !!dataset));
  r.unshift({ r: key, p: path, d: dataset ? 1 : 0 });
  r = r.slice(0, 12);
  try { localStorage.setItem("pqv-recent", JSON.stringify(r)); } catch { /* storage unavailable */ }
  renderRecent();
}
function renderRecent() {
  const r = recentList().filter((x) => x.r === S.cfg.root);
  $("#recent-box").hidden = !r.length;
  $("#recent").innerHTML = r.map((x, i) => `<li data-i="${i}" title="${esc(x.p)}">${x.d ? "⧉ " : ""}${esc(x.p || S.cfg.root_name)}</li>`).join("");
  $$("#recent li").forEach((li) => (li.onclick = () => { const x = r[+li.dataset.i]; openTable(x.p, !!x.d); }));
}

/* ---------------------------------------------------------------- open a table */
async function openTable(path, dataset, view, tab) {
  try {
    const info = await api("info", { params: { path, dataset: dataset ? 1 : 0 }, channel: "info" });
    S.table = { path: info.path, dataset, info };
    S.view = view ? { ...freshView(), ...view } : freshView();
    S.result = null; S.selRow = -1;
    $("#page-size").value = String(S.view.limit);
    $("#search").value = S.view.search || "";
    $("#sql-where").value = S.view.where || "";
    $("#sql-row").hidden = !S.view.where;
    $("#btn-sql").classList.toggle("on", !!S.view.where);
    $("#empty").hidden = true;
    $("#table-view").hidden = false;
    closeDrawer();
    renderHeader();
    $("#schema-body").innerHTML = ""; $("#summary-body").innerHTML = "";
    pushRecent(info.path, dataset);
    // show the containing folder in the sidebar
    const dir = dataset ? info.path : info.path.split("/").slice(0, -1).join("/");
    if (S.dir !== dir || !S.listing) await loadDir(dir, true); else renderDir();
    setTab(tab || "data");
    if (S.tab !== "data") runQuery();
  } catch (e) {
    if (!isAbort(e)) toast(e.message, true);
    saveHash();   // keep the address bar on what is actually shown
  }
}

function renderHeader() {
  const I = S.table.info;
  $("#t-name").textContent = (S.table.dataset ? "⧉ " : "") + (I.name || S.cfg.root_name);
  $("#t-path").textContent = S.cfg.root + (I.path ? "/" + I.path : "");
  const chips = [`<span class="chip"><b>${fmtCount(I.rows)}</b> rows</span>`, `<span class="chip"><b>${fmtCount(I.columns.length)}</b> columns</span>`];
  if (I.n_files != null) chips.push(`<span class="chip"><b>${fmtCount(I.n_files)}</b> files</span>`);
  chips.push(`<span class="chip">${fmtBytes(I.size)}</span>`);
  if (I.file_meta && I.file_meta.num_row_groups != null) chips.push(`<span class="chip">${fmtCount(I.file_meta.num_row_groups)} row group${I.file_meta.num_row_groups === 1 ? "" : "s"}</span>`);
  if (I.partitions && I.partitions.length) chips.push(`<span class="chip">partitions: <b>${esc(I.partitions.join(", "))}</b></span>`);
  if (I.mtime) chips.push(`<span class="chip">modified ${fmtTime(I.mtime)}</span>`);
  $("#t-meta").innerHTML = chips.join("");
}

function setTab(tab) {
  S.tab = tab;
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  for (const t of ["data", "schema", "summary"]) $("#tab-" + t).hidden = t !== tab;
  if (tab === "data") runQuery();
  if (tab === "schema") renderSchema();
  if (tab === "summary" && !$("#summary-body").innerHTML) $("#summary-body").innerHTML = `<p class="muted">Press <b>Run summary</b>. On large tables this reads every row, so it can take a few seconds.</p>`;
  saveHash();
}

/* ---------------------------------------------------------------- data grid */
function spec(extra) {
  const v = S.view;
  return { path: S.table.path, dataset: S.table.dataset, columns: v.columns, filters: v.filters, where: v.where, search: v.search, sort: v.sort, offset: v.offset, limit: v.limit, ...extra };
}
const colKind = (name) => { const c = S.table.info.columns.find((x) => x.name === name); return c ? c.kind : "other"; };
const colType = (name) => { const c = S.table.info.columns.find((x) => x.name === name); return c ? c.type : ""; };

async function runQuery() {
  if (!S.table) return;
  renderChips();
  updateColsButton();
  saveHash();
  $("#grid").classList.add("loading");
  const msg = $("#grid-msg");
  try {
    const r = await api("query", { body: spec(), channel: "grid" });
    S.result = r; S.selRow = -1;
    msg.hidden = true; msg.classList.remove("err");
    renderGrid();
  } catch (e) {
    if (isAbort(e)) return;
    msg.hidden = false; msg.classList.add("err"); msg.textContent = e.message;
    $("#grid tbody").innerHTML = "";
    $("#pager-info").textContent = "";
  } finally {
    $("#grid").classList.remove("loading");
  }
}

function renderGrid() {
  const r = S.result, v = S.view;
  const sortIdx = {};
  v.sort.forEach((s, i) => (sortIdx[s.col] = { i, desc: s.desc }));
  const head = ['<tr><th class="rn">#</th>'];
  for (const c of r.columns) {
    const so = sortIdx[c.name];
    const arrow = so ? `<span class="so">${so.desc ? "▼" : "▲"}${v.sort.length > 1 ? so.i + 1 : ""}</span>` : "";
    head.push(`<th data-col="${esc(c.name)}" title="${esc(c.name)} · ${esc(c.type)}\nClick: sort · Shift+click: add to sort"><div class="h"><span class="hn">${esc(c.name)}</span>${arrow}<button class="mn" title="Column menu">▾</button></div><span class="ht">${esc(c.type)}</span></th>`);
  }
  head.push("</tr>");
  $("#grid thead").innerHTML = head.join("");

  const kinds = r.columns.map((c) => c.kind);
  const body = r.rows.map((row, i) => `<tr data-i="${i}"><td class="rn">${fmtCount(r.offset + i + 1)}</td>${row.map((x, j) => cellHTML(x, kinds[j])[0]).join("")}</tr>`);
  $("#grid tbody").innerHTML = body.join("");
  const msg = $("#grid-msg");
  if (!r.rows.length) { msg.hidden = false; msg.textContent = r.total ? "No rows on this page." : "No rows match."; }

  $$("#grid thead th[data-col]").forEach((th) => {
    th.onclick = (ev) => {
      if (ev.target.classList.contains("mn")) return colMenu(th.dataset.col, ev.target);
      toggleSort(th.dataset.col, ev.shiftKey);
    };
  });
  $$("#grid tbody tr").forEach((tr) => (tr.onclick = () => showRow(+tr.dataset.i)));

  const from = r.total ? r.offset + 1 : 0, to = Math.min(r.offset + r.rows.length, r.total);
  let info = `${fmtCount(from)}–${fmtCount(to)} of ${fmtCount(r.total)} rows`;
  if (r.unfiltered !== r.total) info += ` (filtered from ${fmtCount(r.unfiltered)})`;
  info += ` · ${r.columns.length} of ${S.table.info.columns.length} columns · ${r.elapsed.toFixed(2)} s`;
  $("#pager-info").textContent = info;
  const pages = Math.max(1, Math.ceil(r.total / r.limit)), page = Math.floor(r.offset / r.limit) + 1;
  $("#pg-total").textContent = fmtCount(pages);
  $("#pg-num").value = page; $("#pg-num").max = pages;
  $("#pg-first").disabled = $("#pg-prev").disabled = page <= 1;
  $("#pg-next").disabled = $("#pg-last").disabled = page >= pages;
}

function toggleSort(col, add) {
  const v = S.view;
  const cur = v.sort.find((s) => s.col === col);
  let next;
  if (!cur) next = { col, desc: false };
  else if (!cur.desc) next = { col, desc: true };
  else next = null;
  if (add) {
    v.sort = v.sort.filter((s) => s.col !== col);
    if (next) v.sort.push(next);
  } else v.sort = next ? [next] : [];
  v.offset = 0;
  runQuery();
}
function setSort(col, desc) { S.view.sort = [{ col, desc }]; S.view.offset = 0; runQuery(); }

function gotoPage(p) {
  const r = S.result; if (!r) return;
  const pages = Math.max(1, Math.ceil(r.total / r.limit));
  p = Math.min(Math.max(1, p), pages);
  S.view.offset = (p - 1) * S.view.limit;
  runQuery();
}

/* ---------------------------------------------------------------- filters */
function filterText(f) {
  const op = OPS[f.op] || f.op;
  if (NO_VALUE.has(f.op)) return `${esc(f.col)}<i>${op}</i>`;
  if (f.op === "between") return `${esc(f.col)}<i>between</i>${esc(f.value)}<i>and</i>${esc(f.value2)}`;
  return `${esc(f.col)}<i>${esc(op)}</i>${esc(f.value)}`;
}
function renderChips() {
  const v = S.view, out = [];
  v.filters.forEach((f, i) => out.push(`<span class="fchip${f.enabled === false ? " off" : ""}" data-i="${i}"><span class="t" title="Click to edit">${filterText(f)}</span><button data-act="tog" title="${f.enabled === false ? "Enable" : "Disable"}">${f.enabled === false ? "◯" : "●"}</button><button data-act="del" title="Remove">✕</button></span>`));
  if (v.where) out.push(`<span class="fchip" data-w="1"><span class="t" title="SQL condition"><i>SQL</i>${esc(v.where)}</span><button data-act="delw" title="Remove">✕</button></span>`);
  if (v.search) out.push(`<span class="fchip" data-s="1"><span class="t"><i>search</i>${esc(v.search)}</span><button data-act="dels" title="Remove">✕</button></span>`);
  if (out.length > 1) out.push(`<button class="clear-all" id="clear-all">clear all</button>`);
  $("#filter-chips").innerHTML = out.join("");
  $$("#filter-chips .fchip").forEach((ch) => {
    const i = ch.dataset.i !== undefined ? +ch.dataset.i : -1;
    $(".t", ch).onclick = (ev) => {
      if (i >= 0) filterEditor(ev.currentTarget, v.filters[i], i);
      else if (ch.dataset.w) { $("#sql-row").hidden = false; $("#sql-where").focus(); }
      else $("#search").focus();
    };
    $$("button", ch).forEach((b) => (b.onclick = () => {
      if (b.dataset.act === "del") v.filters.splice(i, 1);
      if (b.dataset.act === "tog") v.filters[i].enabled = v.filters[i].enabled === false;
      if (b.dataset.act === "delw") { v.where = ""; $("#sql-where").value = ""; $("#btn-sql").classList.remove("on"); }
      if (b.dataset.act === "dels") { v.search = ""; $("#search").value = ""; }
      v.offset = 0; runQuery();
    }));
  });
  const ca = $("#clear-all");
  if (ca) ca.onclick = () => { v.filters = []; v.where = ""; v.search = ""; $("#search").value = ""; $("#sql-where").value = ""; $("#btn-sql").classList.remove("on"); v.offset = 0; runQuery(); };
}

function addFilter(f) { S.view.filters.push(f); S.view.offset = 0; runQuery(); }

function filterEditor(anchor, existing, index, presetCol) {
  const cols = S.table.info.columns;
  const f = existing ? { ...existing } : { col: presetCol || (S.result && S.result.columns[0] ? S.result.columns[0].name : cols[0].name), op: null, value: "", value2: "" };
  const pop = openPop(anchor, `
    <label>Column</label>
    <input list="pqv-cols" id="fe-col" value="${esc(f.col)}" autocomplete="off">
    <datalist id="pqv-cols">${cols.map((c) => `<option value="${esc(c.name)}">${esc(c.type)}</option>`).join("")}</datalist>
    <label>Condition</label><select id="fe-op"></select>
    <div id="fe-vals"></div>
    <div class="hint" id="fe-hint"></div>
    <div class="row"><button class="btn ghost" id="fe-cancel">Cancel</button><button class="btn primary" id="fe-ok">${existing ? "Update" : "Add filter"}</button></div>`);
  const colIn = $("#fe-col", pop), opSel = $("#fe-op", pop), vals = $("#fe-vals", pop), hint = $("#fe-hint", pop);
  function refreshOps() {
    const k = colKind(colIn.value);
    const ops = opsFor(k);
    const keep = f.op && ops.includes(f.op) ? f.op : ops[0];
    opSel.innerHTML = ops.map((o) => `<option value="${o}"${o === keep ? " selected" : ""}>${esc(OPS[o])}</option>`).join("");
    refreshVals();
  }
  function refreshVals() {
    const op = opSel.value, k = colKind(colIn.value), t = colType(colIn.value);
    f.op = op;
    if (NO_VALUE.has(op)) vals.innerHTML = "";
    else if (op === "between") vals.innerHTML = `<label>From</label><input id="fe-v1" value="${esc(f.value || "")}"><label>To</label><input id="fe-v2" value="${esc(f.value2 || "")}">`;
    else if (op === "in" || op === "not_in") vals.innerHTML = `<label>Values (comma or new line separated)</label><textarea id="fe-v1">${esc(f.value || "")}</textarea>`;
    else vals.innerHTML = `<label>Value</label><input id="fe-v1" value="${esc(f.value || "")}">`;
    hint.textContent = t ? `${colIn.value}: ${t}` + (k === "date" || k === "timestamp" ? " — dates as 2024-06-28" : k === "bool" ? " — true / false" : "") : "Pick a column from the list";
    $$("input,textarea", vals).forEach((el) => el.addEventListener("keydown", (e) => { if (e.key === "Enter" && el.tagName === "INPUT") ok(); }));
    const first = $("#fe-v1", pop); if (first) first.focus();
  }
  function ok() {
    const col = colIn.value;
    if (!S.table.info.columns.some((c) => c.name === col)) { hint.textContent = "Unknown column: " + col; return; }
    const nf_ = { col, op: opSel.value, value: ($("#fe-v1", pop) || {}).value || "", value2: ($("#fe-v2", pop) || {}).value || "" };
    if (existing && existing.enabled === false) nf_.enabled = false;
    closePop();
    if (index != null) { S.view.filters[index] = nf_; S.view.offset = 0; runQuery(); } else addFilter(nf_);
  }
  colIn.addEventListener("change", refreshOps);
  colIn.addEventListener("input", () => { if (S.table.info.columns.some((c) => c.name === colIn.value)) refreshOps(); });
  opSel.onchange = refreshVals;
  $("#fe-cancel", pop).onclick = closePop;
  $("#fe-ok", pop).onclick = ok;
  refreshOps();
  if (!existing && !presetCol) { colIn.select(); colIn.focus(); }
}

/* ---------------------------------------------------------------- columns */
function updateColsButton() {
  const n = S.table.info.columns.length, k = S.view.columns ? S.view.columns.length : n;
  $("#btn-cols").textContent = k === n ? `Columns (${n})` : `Columns (${k} of ${n})`;
  $("#btn-cols").classList.toggle("on", k !== n);
}
function columnsPicker(anchor) {
  const all = S.table.info.columns;
  let chosen = new Set(S.view.columns || all.map((c) => c.name));
  const pop = openPop(anchor, `
    <input type="search" id="cp-q" placeholder="Find column…" autocomplete="off">
    <div class="links"><a id="cp-all">all</a><a id="cp-none">none</a><a id="cp-match">only matching</a></div>
    <div class="col-list" id="cp-list"></div>
    <div class="row"><button class="btn ghost" id="cp-cancel">Cancel</button><button class="btn primary" id="cp-ok">Apply</button></div>`);
  const q = $("#cp-q", pop), list = $("#cp-list", pop);
  const matching = () => { const s = q.value.trim().toLowerCase(); return all.filter((c) => !s || c.name.toLowerCase().includes(s)); };
  function render() {
    list.innerHTML = matching().map((c) => `<label><input type="checkbox" value="${esc(c.name)}"${chosen.has(c.name) ? " checked" : ""}>${esc(c.name)}<small>${esc(c.type)}</small></label>`).join("");
    $$("input", list).forEach((cb) => (cb.onchange = () => (cb.checked ? chosen.add(cb.value) : chosen.delete(cb.value))));
  }
  q.oninput = render;
  $("#cp-all", pop).onclick = () => { matching().forEach((c) => chosen.add(c.name)); render(); };
  $("#cp-none", pop).onclick = () => { matching().forEach((c) => chosen.delete(c.name)); render(); };
  $("#cp-match", pop).onclick = () => { chosen = new Set(matching().map((c) => c.name)); render(); };
  $("#cp-cancel", pop).onclick = closePop;
  $("#cp-ok", pop).onclick = () => {
    const cols = all.map((c) => c.name).filter((c) => chosen.has(c));
    if (!cols.length) { toast("Pick at least one column", true); return; }
    S.view.columns = cols.length === all.length ? null : cols;
    S.view.sort = S.view.sort.filter((s) => cols.includes(s.col));
    closePop(); runQuery();
  };
  render(); q.focus();
}
function hideColumn(col) {
  const cur = S.view.columns || S.table.info.columns.map((c) => c.name);
  const next = cur.filter((c) => c !== col);
  if (!next.length) return toast("Can't hide the last column", true);
  S.view.columns = next; runQuery();
}

function colMenu(col, anchor) {
  const pop = openPop(anchor, `<div class="menu">
    <button data-a="asc">Sort ascending</button><button data-a="desc">Sort descending</button><hr>
    <button data-a="filter">Filter…</button><button data-a="stats">Statistics…</button><hr>
    <button data-a="hide">Hide column</button><button data-a="copy">Copy name</button></div>`);
  $$("button", pop).forEach((b) => (b.onclick = () => {
    const a = b.dataset.a;
    if (a === "filter") { filterEditor(anchor, null, null, col); return; }
    closePop();
    if (a === "asc") setSort(col, false);
    if (a === "desc") setSort(col, true);
    if (a === "stats") showStats(col);
    if (a === "hide") hideColumn(col);
    if (a === "copy") copyText(col);
  }));
}

/* ---------------------------------------------------------------- popovers & modal */
function openPop(anchor, html) {
  const pop = $("#pop");
  pop.innerHTML = html; pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  pop.style.left = "0px"; pop.style.top = "0px";
  const w = pop.offsetWidth, h = pop.offsetHeight;
  let left = Math.min(r.left, window.innerWidth - w - 8), top = r.bottom + 4;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
  pop.style.left = Math.max(8, left) + "px"; pop.style.top = top + "px";
  setTimeout(() => document.addEventListener("mousedown", outside), 0);
  return pop;
}
function outside(e) { if (!$("#pop").contains(e.target)) closePop(); }
function closePop() { $("#pop").hidden = true; $("#pop").innerHTML = ""; document.removeEventListener("mousedown", outside); }
function openModal(html) { $("#modal-box").innerHTML = html; $("#modal").hidden = false; }
function closeModal() { $("#modal").hidden = true; $("#modal-box").innerHTML = ""; }
async function copyText(t) {
  try { await navigator.clipboard.writeText(t); toast("Copied"); }
  catch { const ta = document.createElement("textarea"); ta.value = t; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); toast("Copied"); }
}

/* ---------------------------------------------------------------- drawer: row detail & stats */
function openDrawer(title, sub, html) {
  $("#drawer").hidden = false;
  $("#drawer-title").textContent = title; $("#drawer-sub").textContent = sub || "";
  $("#drawer-body").innerHTML = html;
  $("#drawer-body").scrollTop = 0;
}
function closeDrawer() { $("#drawer").hidden = true; $$("#grid tbody tr.sel").forEach((t) => t.classList.remove("sel")); S.selRow = -1; }

function showRow(i) {
  const r = S.result; if (!r || !r.rows[i]) return;
  S.selRow = i;
  $$("#grid tbody tr").forEach((tr) => tr.classList.toggle("sel", +tr.dataset.i === i));
  const row = r.rows[i];
  const fields = r.columns.map((c, j) => {
    const v = plainValue(row[j]);
    return `<div class="fk" data-n="${esc(c.name.toLowerCase())}">${esc(c.name)}<small>${esc(c.type)}</small></div><div class="fv${v === null ? " nul" : ""}" data-n="${esc(c.name.toLowerCase())}">${v === null ? "null" : esc(v)}${v === null ? "" : `<button data-j="${j}" title="Filter on this value">⊕</button>`}</div>`;
  }).join("");
  const hiddenCols = S.table.info.columns.length - r.columns.length;
  openDrawer(`Row ${fmtCount(r.offset + i + 1)}`, `${r.columns.length} fields${hiddenCols ? ` (${hiddenCols} hidden columns not shown)` : ""} · ↑/↓ to move`,
    `<input class="row-search" id="row-q" type="search" placeholder="Find field…"><div class="fields" id="row-fields">${fields}</div>`);
  $("#row-q").oninput = (e) => {
    const s = e.target.value.trim().toLowerCase();
    $$("#row-fields > div").forEach((d) => (d.hidden = !!s && !d.dataset.n.includes(s)));
  };
  $$("#row-fields button").forEach((b) => (b.onclick = () => {
    const j = +b.dataset.j, c = r.columns[j], v = row[j];
    addFilter({ col: c.name, op: "=", value: typeof v === "object" ? JSON.stringify(v) : String(v).replace(/ 00:00:00$/, "") });
  }));
  const tr = $(`#grid tbody tr[data-i="${i}"]`), wrap = $("#grid-wrap");
  if (tr) {   // keep the row visible without scrolling sideways
    const top = tr.offsetTop - $("#grid thead").offsetHeight, bottom = tr.offsetTop + tr.offsetHeight;
    if (top < wrap.scrollTop) wrap.scrollTop = top;
    else if (bottom > wrap.scrollTop + wrap.clientHeight) wrap.scrollTop = bottom - wrap.clientHeight;
  }
}

async function showStats(col) {
  openDrawer(col, colType(col) + " · loading…", `<p class="muted">Computing statistics…</p>`);
  try {
    const st = await api("stats", { body: spec({ column: col }), channel: "stats" });
    renderStats(st);
  } catch (e) {
    if (!isAbort(e)) $("#drawer-body").innerHTML = `<p class="grid-msg err" style="position:static">${esc(e.message)}</p>`;
  }
}

function statBox(k, v) { return `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${esc(fmtStat(v))}</div></div>`; }
function renderStats(st) {
  const filtered = S.view.filters.length || S.view.where || S.view.search;
  $("#drawer-sub").textContent = `${st.type}${filtered ? " · with current filters" : ""} · ${st.elapsed.toFixed(2)} s`;
  const pct = (n) => (st.rows ? ` (${(100 * n / st.rows).toFixed(1)}%)` : "");
  let h = `<div class="stat-grid">` + statBox("rows", st.rows) + `<div class="stat"><div class="k">nulls</div><div class="v">${fmtCount(st.nulls)}${pct(st.nulls)}</div></div>` + statBox("distinct (approx.)", st.distinct);
  if (st.kind === "float") h += `<div class="stat"><div class="k">NaN</div><div class="v">${fmtCount(st.nan)}${pct(st.nan)}</div></div>`;
  if (NUMERIC.has(st.kind)) {
    h += statBox("min", st.min) + statBox("max", st.max) + statBox("mean", st.mean) + statBox("std dev", st.std) + statBox("sum", st.sum) + statBox("zeros", st.zeros) + statBox("negative", st.negative);
  } else if (st.kind === "string") {
    h += statBox("min", st.min) + statBox("max", st.max) + statBox("empty strings", st.empty) + statBox("length min / max", `${st.min_len ?? "–"} / ${st.max_len ?? "–"}`) + statBox("mean length", st.mean_len);
  } else if (st.min !== undefined) {
    h += statBox("min", st.min) + statBox("max", st.max);
  }
  h += `</div>`;
  if (st.histogram && st.histogram.bins.length) h += `<div class="sect">Distribution</div>` + histSVG(st.histogram);
  if (st.quantiles) h += `<div class="sect">Percentiles</div><table class="plain">` + Object.entries(st.quantiles).map(([k, v]) => `<tr><td>${k}</td><td class="num">${esc(fmtStat(v))}</td></tr>`).join("") + `</table>`;
  if (st.periods && st.periods.bins.length) h += `<div class="sect">Rows per ${st.periods.unit}</div>` + periodSVG(st.periods) + periodTable(st.periods);
  if (st.top && st.top.length) {
    const mx = Math.max(...st.top.map((x) => x[1]));
    h += `<div class="sect">Most frequent values <span class="muted" style="font-weight:400">(click to filter)</span></div><ul class="top-list">` +
      st.top.map(([v, c], i) => `<li data-i="${i}"><span class="bar" style="width:${(100 * c / mx).toFixed(1)}%"></span><span class="v">${v === null ? "<i>null</i>" : esc(typeof v === "object" ? JSON.stringify(v) : typeof v === "number" ? fmtNum(v) : v)}</span><span class="c">${fmtCount(c)}${pct(c)}</span></li>`).join("") + `</ul>`;
  }
  $("#drawer-body").innerHTML = h;
  $$("#drawer-body .top-list li").forEach((li) => (li.onclick = () => {
    const v = st.top[+li.dataset.i][0];
    if (v === null) addFilter({ col: st.column, op: "null" });
    else addFilter({ col: st.column, op: "=", value: typeof v === "object" ? JSON.stringify(v) : String(v).replace(/ 00:00:00$/, "") });
  }));
  $$("#drawer-body rect[data-lo]").forEach((r) => (r.onclick = () => addFilter({ col: st.column, op: "between", value: r.dataset.lo, value2: r.dataset.hi })));
}

function histSVG(H) {
  const W = 352, Ht = 120, pad = 18;
  const bins = H.bins.slice();
  const extra = [H.below, H.above];
  const n = bins.length + (H.below ? 1 : 0) + (H.above ? 1 : 0);
  const mx = Math.max(1, ...bins, ...extra);
  const bw = W / n;
  let x = 0, out = [];
  const bar = (c, cls, title, lo, hi) => {
    const h = (Ht - pad) * c / mx;
    const data = lo !== undefined ? ` data-lo="${lo}" data-hi="${hi}" style="cursor:pointer"` : "";
    out.push(`<rect class="${cls}" x="${(x + 0.5).toFixed(1)}" y="${(Ht - pad - h).toFixed(1)}" width="${Math.max(1, bw - 1).toFixed(1)}" height="${Math.max(c ? 1 : 0, h).toFixed(1)}"${data}><title>${esc(title)}</title></rect>`);
    x += bw;
  };
  if (H.below) bar(H.below, "o", `below ${fmtNum(H.lo)}: ${fmtCount(H.below)}`);
  bins.forEach((c, i) => {
    const lo = H.lo + i * H.width, hi = lo + H.width;
    bar(c, "b", `${fmtNum(lo)} – ${fmtNum(hi)}: ${fmtCount(c)}`, fmtNum(lo), fmtNum(hi));
  });
  if (H.above) bar(H.above, "o", `above ${fmtNum(H.hi)}: ${fmtCount(H.above)}`);
  out.push(`<text x="${H.below ? bw : 0}" y="${Ht - 4}">${esc(fmtNum(H.lo))}</text><text x="${W - (H.above ? bw : 0)}" y="${Ht - 4}" text-anchor="end">${esc(fmtNum(H.hi))}</text>`);
  const note = H.clipped ? `<div class="muted" style="font-size:11px">Range trimmed to the 0.5%–99.5% percentiles; pale bars hold the tails. Click a bar to filter.</div>` : `<div class="muted" style="font-size:11px">Click a bar to filter to that range.</div>`;
  return `<svg class="hist" viewBox="0 0 ${W} ${Ht}" preserveAspectRatio="none">${out.join("")}</svg>${note}`;
}
function periodSVG(P) {
  const W = 352, Ht = 100, bins = P.bins, mx = Math.max(1, ...bins.map((b) => b[1])), bw = W / bins.length;
  const rects = bins.map(([d, c], i) => { const h = (Ht - 16) * c / mx; return `<rect class="b" x="${(i * bw).toFixed(1)}" y="${(Ht - 16 - h).toFixed(1)}" width="${Math.max(1, bw - (bw > 3 ? 1 : 0)).toFixed(1)}" height="${h.toFixed(1)}"><title>${esc(d)}: ${fmtCount(c)}</title></rect>`; });
  rects.push(`<text x="0" y="${Ht - 3}">${esc(bins[0][0])}</text><text x="${W}" y="${Ht - 3}" text-anchor="end">${esc(bins[bins.length - 1][0])}</text>`);
  return `<svg class="hist" viewBox="0 0 ${W} ${Ht}" preserveAspectRatio="none" style="height:100px">${rects.join("")}</svg>`;
}
function periodTable(P) {
  const rows = P.bins.slice().reverse().slice(0, 240);
  return `<details class="meta-d"><summary>table (${P.bins.length} ${P.unit}s)</summary><table class="plain">${rows.map(([d, c]) => `<tr><td class="mono">${esc(P.unit === "year" ? d.slice(0, 4) : P.unit === "month" ? d.slice(0, 7) : d)}</td><td class="num">${fmtCount(c)}</td></tr>`).join("")}</table></details>`;
}

/* ---------------------------------------------------------------- schema tab */
function renderSchema() {
  const I = S.table.info;
  const meta = {};
  (I.column_meta || []).forEach((m) => (meta[m.path] = m));
  const maxC = Math.max(1, ...(I.column_meta || []).map((m) => m.compressed || 0));
  const q = $("#schema-filter").value.trim().toLowerCase();
  const cols = I.columns.map((c, i) => ({ ...c, i })).filter((c) => !q || c.name.toLowerCase().includes(q) || c.type.toLowerCase().includes(q));
  const hasMeta = !!(I.column_meta && I.column_meta.length);
  let h = `<table class="plain"><thead><tr><th>#</th><th>Column</th><th>Type</th>` + (hasMeta ? `<th>Parquet type</th><th>Compression</th><th style="text-align:right">Compressed</th><th style="text-align:right">Uncompressed</th><th style="text-align:right">Nulls</th>` : "") + `</tr></thead><tbody>`;
  for (const c of cols) {
    const m = meta[c.name] || {};
    h += `<tr class="clickable" data-col="${esc(c.name)}" title="Click for statistics"><td class="num">${c.i + 1}</td><td class="mono">${esc(c.name)}</td><td class="mono">${esc(c.type)}</td>` +
      (hasMeta ? `<td class="mono">${esc(m.physical || "")}</td><td class="mono">${esc(m.compression || "")}</td><td class="num">${m.compressed != null ? fmtBytes(m.compressed) + `<span class="sizebar" style="width:${(40 * m.compressed / maxC).toFixed(0)}px"></span>` : ""}</td><td class="num">${m.uncompressed != null ? fmtBytes(m.uncompressed) : ""}</td><td class="num">${m.nulls != null ? fmtCount(m.nulls) : ""}</td>` : "") + `</tr>`;
  }
  h += `</tbody></table>`;
  $("#schema-count").textContent = `${cols.length} of ${I.columns.length} columns`;

  if (I.file_meta) {
    h += `<div class="sect">File</div><dl class="kv">` + Object.entries(I.file_meta).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("") + `<dt>size</dt><dd>${fmtBytes(I.size)}</dd><dt>modified</dt><dd>${fmtTime(I.mtime)}</dd></dl>`;
  }
  if (I.n_files != null) {
    h += `<div class="sect">Folder as table</div><dl class="kv"><dt>files</dt><dd>${fmtCount(I.n_files)}</dd><dt>total size</dt><dd>${fmtBytes(I.size)}</dd><dt>partition columns</dt><dd>${esc((I.partitions || []).join(", ") || "none")}</dd><dt>first files</dt><dd>${(I.sample_files || []).map(esc).join("<br>")}</dd></dl><p class="muted">Column <code>_file</code> shows which file each row came from.</p>`;
  }
  if (I.kv_meta && I.kv_meta.length) {
    h += `<div class="sect">Key / value metadata</div>`;
    for (const kv of I.kv_meta) {
      let v = kv.value;
      try { v = JSON.stringify(JSON.parse(v), null, 2); } catch { /* not JSON */ }
      h += `<details class="meta-d"><summary>${esc(kv.key)} <span class="muted">(${fmtBytes(kv.value.length)})</span></summary><pre class="meta">${esc(v)}</pre></details>`;
    }
  }
  $("#schema-body").innerHTML = h;
  $$("#schema-body tr.clickable").forEach((tr) => (tr.onclick = () => showStats(tr.dataset.col)));
}

/* ---------------------------------------------------------------- summary tab */
async function runSummary() {
  const body = $("#summary-body");
  body.innerHTML = `<p class="muted">Summarising…</p>`;
  try {
    const r = await api("summarize", { body: spec(), channel: "summary" });
    const numCols = new Set(["min", "max", "approx_unique", "avg", "std", "q25", "q50", "q75", "count", "null_percentage"]);
    let h = `<p class="muted">${r.rows.length} columns · ${r.elapsed.toFixed(2)} s${S.view.filters.length || S.view.where || S.view.search ? " · with current filters" : ""}. Click a row for details.</p><table class="plain"><thead><tr>` + r.columns.map((c) => `<th${numCols.has(c) ? ' style="text-align:right"' : ""}>${esc(c.replace("_", " "))}</th>`).join("") + `</tr></thead><tbody>`;
    for (const row of r.rows) {
      h += `<tr class="clickable" data-col="${esc(row[0])}">` + row.map((v, j) => {
        const c = r.columns[j];
        let s = v == null ? "" : typeof v === "number" ? (c === "null_percentage" ? v.toFixed(2) + "%" : fmtStat(v)) : String(v);
        if (c === "null_percentage" && typeof v === "string") s = v;
        if (typeof v === "string" && /^-?\d+(\.\d+)?(e[-+]?\d+)?$/i.test(v) && v.length > 12) s = fmtNum(parseFloat(v));
        return `<td class="${numCols.has(c) ? "num" : "mono"}">${esc(s.length > 60 ? s.slice(0, 60) + "…" : s)}</td>`;
      }).join("") + `</tr>`;
    }
    body.innerHTML = h + `</tbody></table>`;
    $$("#summary-body tr.clickable").forEach((tr) => (tr.onclick = () => showStats(tr.dataset.col)));
  } catch (e) {
    if (!isAbort(e)) body.innerHTML = `<p class="grid-msg err" style="position:static">${esc(e.message)}</p>`;
  }
}

/* ---------------------------------------------------------------- export & code */
async function doExport() {
  try {
    const r = await api("export", { body: spec() });
    const total = S.result ? S.result.total : null;
    if (total > 2_000_000 && !confirm(`Export ${fmtCount(total)} rows to CSV? This can be a large file.`)) return;
    const a = document.createElement("a");
    a.href = r.url; a.download = r.filename; document.body.appendChild(a); a.click(); a.remove();
    toast(`Downloading ${r.filename}${total != null ? ` (${fmtCount(total)} rows)` : ""}`);
  } catch (e) { toast(e.message, true); }
}
async function showCode() {
  try {
    const r = await api("code", { body: spec() });
    openModal(`<h2>This view as code</h2>
      <div class="sect">DuckDB SQL</div><pre id="code-sql">${esc(r.sql)}</pre>
      <div class="row"><span class="muted">Paths are absolute on this computer.</span><button class="btn" id="cp-sql">Copy SQL</button></div>
      <div class="sect">Python</div><pre id="code-py">${esc(r.python)}</pre>
      <div class="row"><span></span><span><button class="btn" id="cp-py">Copy Python</button> <button class="btn primary" id="code-close">Close</button></span></div>`);
    $("#cp-sql").onclick = () => copyText(r.sql);
    $("#cp-py").onclick = () => copyText(r.python);
    $("#code-close").onclick = closeModal;
  } catch (e) { toast(e.message, true); }
}

/* ---------------------------------------------------------------- wiring */
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function wire() {
  $("#dir-filter").oninput = () => S.listing && renderDir();
  $("#btn-refresh").onclick = () => { loadDir(S.dir); if (S.table) openTable(S.table.path, S.table.dataset, S.view, S.tab); };
  $$(".tabs button").forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));
  $("#search").oninput = debounce(() => { const v = $("#search").value.trim(); if (v === S.view.search) return; S.view.search = v; S.view.offset = 0; runQuery(); }, 350);
  $("#btn-filter").onclick = (e) => filterEditor(e.currentTarget, null, null);
  $("#btn-cols").onclick = (e) => columnsPicker(e.currentTarget);
  $("#btn-sql").onclick = () => { const r = $("#sql-row"); r.hidden = !r.hidden; if (!r.hidden) $("#sql-where").focus(); };
  const applySql = () => { S.view.where = $("#sql-where").value.trim(); $("#btn-sql").classList.toggle("on", !!S.view.where); S.view.offset = 0; runQuery(); };
  $("#sql-apply").onclick = applySql;
  $("#sql-where").onkeydown = (e) => { if (e.key === "Enter") applySql(); };
  $("#sql-clear").onclick = () => { $("#sql-where").value = ""; applySql(); $("#sql-row").hidden = true; };
  $("#page-size").onchange = () => { S.view.limit = +$("#page-size").value; S.view.offset = 0; runQuery(); };
  $("#pg-first").onclick = () => gotoPage(1);
  $("#pg-prev").onclick = () => gotoPage(Math.floor(S.view.offset / S.view.limit));
  $("#pg-next").onclick = () => gotoPage(Math.floor(S.view.offset / S.view.limit) + 2);
  $("#pg-last").onclick = () => gotoPage(Infinity);
  $("#pg-num").onchange = () => gotoPage(+$("#pg-num").value || 1);
  $("#btn-export").onclick = doExport;
  $("#btn-code").onclick = showCode;
  $("#btn-summarize").onclick = runSummary;
  $("#schema-filter").oninput = () => S.table && renderSchema();
  $("#drawer-close").onclick = closeDrawer;
  $("#modal").onclick = (e) => { if (e.target.id === "modal") closeModal(); };
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { if (!$("#pop").hidden) closePop(); else if (!$("#modal").hidden) closeModal(); else if (!$("#drawer").hidden) closeDrawer(); }
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
    if (typing || !S.result || S.tab !== "data") return;
    if (S.selRow >= 0 && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      const n = S.selRow + (e.key === "ArrowDown" ? 1 : -1);
      if (n >= 0 && n < S.result.rows.length) { e.preventDefault(); showRow(n); }
    }
    if (e.key === "/" ) { e.preventDefault(); $("#search").focus(); }
  });
}

async function init() {
  wire();
  try {
    S.cfg = await api("config");
  } catch (e) { toast("Cannot reach the server: " + e.message, true); return; }
  $("#root-path").textContent = S.cfg.root;
  document.title = "Parquet Viewer · " + S.cfg.root_name;
  renderRecent();
  await applyHash();
  window.addEventListener("hashchange", applyHash);   // back/forward, edited or pasted links
}

async function applyHash() {
  const st = readHash();
  if (st.p !== undefined) {
    await openTable(st.p, !!st.ds, { columns: st.c || null, filters: st.f || [], where: st.w || "", search: st.s || "", sort: st.o || [], offset: st.of || 0, limit: st.l || 100 }, st.t);
  } else {
    S.table = null; S.result = null;
    $("#table-view").hidden = true; $("#empty").hidden = false; closeDrawer();
    await loadDir(st.d || "", true);
  }
}
init();
