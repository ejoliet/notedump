// notedump viewer: decrypt an export folder in the browser, query it with DuckDB-WASM.
// AIDEV-NOTE: nothing here may send note content over the network. The CSP in
// index.html enforces it; keep new code within connect-src.

import * as duckdb from "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.32.0/+esm";
import { marked } from "https://cdn.jsdelivr.net/npm/marked@18.0.14/lib/marked.esm.js";
import DOMPurify from "https://cdn.jsdelivr.net/npm/dompurify@3.4.15/dist/purify.es.mjs";
import { zipSync, strToU8 } from "https://cdn.jsdelivr.net/npm/fflate@0.8.2/esm/browser.js";
import { deriveKey, decrypt, verifyPassphrase } from "./crypto.js";
import { parseQuery, buildSql, highlightTerms, SORTS, SEP, NOTE_COLS, SQL } from "./query.js";

const PAGE = 300;
const IDLE_LOCK_MS = 15 * 60 * 1000;

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) n.append(kid);
  return n;
};

const state = {
  files: new Map(),     // basename -> File (encrypted or plain, as dropped)
  manifest: null,
  key: null,            // CryptoKey or null for plain exports
  db: null,
  conn: null,
  atts: new Map(),      // attachment id -> row
  blobUrls: new Map(),  // attachment id -> object URL (revoked on lock)
  results: [],
  selected: -1,
  current: null,        // full note row in the reader
  terms: [],
  idleTimer: 0,
};

// ---------- loading ----------

async function collectEntries(items) {
  const files = [];
  const walk = async (entry) => {
    if (entry.isFile) {
      files.push(await new Promise((res, rej) => entry.file(res, rej)));
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        for (const e of batch) await walk(e);
      } while (batch.length);
    }
  };
  for (const it of items) {
    const entry = it.webkitGetAsEntry?.();
    if (entry) await walk(entry);
    else if (it.getAsFile?.()) files.push(it.getAsFile());
  }
  return files;
}

async function addFiles(list) {
  for (const f of list) state.files.set(f.name, f);
  const mf = state.files.get("notedump.json");
  if (!mf) {
    showLoadError("Add the export folder, or at least notedump.json and notes.arrows.gz.enc.");
    return;
  }
  try {
    state.manifest = JSON.parse(await mf.text());
  } catch {
    showLoadError("notedump.json is not valid JSON. Re-export the folder.");
    return;
  }
  if (state.manifest.format !== "notedump") {
    showLoadError("This folder was not made by notedump.");
    return;
  }
  const notesName = state.manifest.files?.notes;
  if (!state.files.has(notesName)) {
    showLoadError(`${notesName} is missing. Select it together with notedump.json.`);
    return;
  }
  $("#load-error").textContent = "";
  const nFiles = [...state.files.keys()].filter((n) => /^[0-9a-f]{32}(\.enc)?$/.test(n)).length;
  $("#load-summary").textContent =
    `Export from ${fmtDate(state.manifest.exported_at)} · ${nFiles} attachment files selected`;
  if (state.manifest.encrypted) {
    $("#unlock-form").hidden = false;
    $("#pass").focus();
  } else {
    await open(null);
  }
}

function showLoadError(msg) {
  $("#load-error").textContent = msg;
}

async function readLogical(name, aad) {
  const f = state.files.get(name);
  if (!f) return null;
  const buf = new Uint8Array(await f.arrayBuffer());
  const plain = state.key ? await decrypt(state.key, buf, aad) : buf;
  return gunzip(plain);
}

async function gunzip(u8) {
  const stream = new Blob([u8]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function unlock(ev) {
  ev.preventDefault();
  const pass = $("#pass").value;
  const btn = $("#unlock-btn");
  btn.disabled = true;
  $("#unlock-status").textContent = "Checking passphrase";
  try {
    const key = await deriveKey(pass, state.manifest);
    if (!(await verifyPassphrase(key, state.manifest))) {
      $("#unlock-status").textContent = "That passphrase does not open this export.";
      return;
    }
    $("#pass").value = "";
    await open(key);
  } catch (err) {
    $("#unlock-status").textContent = String(err.message ?? err);
  } finally {
    btn.disabled = false;
  }
}

async function startDuckDB() {
  const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
  const workerUrl = URL.createObjectURL(
    new Blob([`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" }),
  );
  const worker = new Worker(workerUrl);
  const db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), worker);
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  URL.revokeObjectURL(workerUrl);
  return db;
}

async function open(key) {
  state.key = key;
  $("#unlock-status").textContent = "Decrypting notes";
  const names = state.manifest.files ?? {};
  const notes = await readLogical(names.notes, "notes.arrows.gz");
  const atts = await readLogical(names.attachments, "attachments.arrows.gz");

  $("#unlock-status").textContent = "Starting the query engine";
  state.db ??= await startDuckDB();
  state.conn = await state.db.connect();
  // Arrow IPC loads without any DuckDB extension (Parquet would need a network fetch).
  await state.conn.insertArrowFromIPCStream(notes, { name: "notes_raw", create: true });
  await state.conn.query(SQL.createNotes);
  if (atts) {
    await state.conn.insertArrowFromIPCStream(atts, { name: "attachments", create: true });
  } else {
    await state.conn.query(SQL.emptyAttachments);
  }
  state.atts = new Map(
    (await rows(SQL.attachments))
      .map((a) => [a.id, a]),
  );

  document.body.dataset.view = "app";
  $("#lock").hidden = !key;
  await renderSidebar();
  await search();
  const fromHash = /^#n(\d+)$/.exec(location.hash);
  if (fromHash) openNote(Number(fromHash[1]));
  $("#q").focus();
  armIdleLock();
}

async function lock() {
  for (const url of state.blobUrls.values()) URL.revokeObjectURL(url);
  state.blobUrls.clear();
  state.atts.clear();
  if (state.conn) {
    await state.conn.query("DROP TABLE IF EXISTS notes; DROP TABLE IF EXISTS attachments;");
    await state.conn.close();
  }
  state.conn = null;
  state.key = null;
  state.results = [];
  state.current = null;
  $("#list").replaceChildren();
  $("#reader-body").replaceChildren();
  $("#folders").replaceChildren();
  $("#tags").replaceChildren();
  document.body.dataset.view = "load";
  $("#unlock-form").hidden = !state.manifest?.encrypted;
  $("#unlock-status").textContent = "Locked. Enter the passphrase to open again.";
  $("#pass").focus();
}

function armIdleLock() {
  clearTimeout(state.idleTimer);
  if (state.key) state.idleTimer = setTimeout(lock, IDLE_LOCK_MS);
}

// ---------- queries ----------

async function rows(sql, params = []) {
  let table;
  if (params.length) {
    const stmt = await state.conn.prepare(sql);
    try { table = await stmt.query(...params); } finally { await stmt.close(); }
  } else {
    table = await state.conn.query(sql);
  }
  return table.toArray().map((r) => r.toJSON());
}

async function search() {
  const q = parseQuery($("#q").value);
  state.terms = highlightTerms(q);
  const sort = $("#sort").value;
  const { where, order, params, orderParams } = buildSql(q, sort);
  try {
    const [{ n }] = await rows(SQL.count(where), params);
    state.results = await rows(SQL.page(where, order, PAGE), [...params, ...orderParams]);
    $("#count").textContent = n > PAGE ? `First ${PAGE} of ${n} notes` : `${n} note${n === 1 ? "" : "s"}`;
    $("#query-error").textContent = "";
  } catch (err) {
    state.results = [];
    $("#query-error").textContent = `Search failed: ${err.message}`;
  }
  renderList();
}

// ---------- rendering ----------

const fmtDate = (v) => {
  if (v == null) return "";
  const d = new Date(v);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
};

function highlightText(text) {
  const frag = document.createDocumentFragment();
  if (!state.terms.length) { frag.append(text); return frag; }
  const re = new RegExp(state.terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "gi");
  let last = 0;
  for (const m of text.matchAll(re)) {
    frag.append(text.slice(last, m.index), el("mark", {}, m[0]));
    last = m.index + m[0].length;
  }
  frag.append(text.slice(last));
  return frag;
}

function snippet(body, title) {
  let text = (body ?? "").replace(/\s+/g, " ").trim();
  if (title && text.toLowerCase().startsWith(title.toLowerCase())) text = text.slice(title.length).trim();
  const lower = text.toLowerCase();
  const hit = state.terms.map((t) => lower.indexOf(t)).filter((i) => i >= 0).sort((a, b) => a - b)[0];
  if (hit === undefined || hit < 60) return text.slice(0, 160);
  return "…" + text.slice(hit - 50, hit + 110);
}

function renderList() {
  const list = $("#list");
  list.replaceChildren(
    ...state.results.map((r, i) =>
      el("li", { role: "option", id: `r${r.id}`, "aria-selected": i === state.selected,
        onclick: () => select(i) },
        el("div", { class: "row-title" }, r.pinned ? el("span", { class: "pin", title: "Pinned" }, "Pinned ") : null,
          highlightText(r.title || "Untitled")),
        el("div", { class: "row-meta" }, `${fmtDate(r.modified_ms)}  ${r.folder}`,
          r.n_att ? `  ${r.n_att} attachment${r.n_att > 1 ? "s" : ""}` : ""),
        el("div", { class: "row-snippet" }, highlightText(snippet(r.body_text, r.title))),
      ),
    ),
  );
  if (!state.results.length) {
    list.append(el("li", { class: "empty" }, "No notes match. Remove a filter or try fewer words."));
  }
}

function select(i) {
  if (i < 0 || i >= state.results.length) return;
  state.selected = i;
  for (const li of $("#list").children) li.setAttribute("aria-selected", "false");
  const li = $("#list").children[i];
  li?.setAttribute("aria-selected", "true");
  li?.scrollIntoView({ block: "nearest" });
  openNote(state.results[i].id);
}

async function openNote(id) {
  const [note] = await rows(SQL.note, [id]);
  if (!note) return;
  note.tags = note.tags_s ? note.tags_s.split(SEP) : [];
  note.attachments = [...state.atts.values()].filter((a) => a.note_id === id);
  state.current = note;
  history.replaceState(null, "", `#n${id}`);
  document.body.dataset.pane = "reader";

  const html = DOMPurify.sanitize(
    marked.parse(note.body_md.replace(/\]\(att:([\w-]+)\)/g, "](#att-$1)"), { gfm: true, breaks: false }),
  );
  const body = $("#reader-body");
  body.innerHTML = html;
  for (const a of body.querySelectorAll("a[href]")) {
    const href = a.getAttribute("href");
    if (href.startsWith("#att-")) {
      const attId = href.slice(5);
      a.addEventListener("click", (e) => { e.preventDefault(); openAttachment(attId); });
    } else if (/^https?:/i.test(href)) {
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    }
  }
  for (const img of body.querySelectorAll('img[src^="#att-"]')) hydrateImage(img);
  markText(body);

  $("#reader-title").textContent = note.title || "Untitled";
  $("#reader-meta").replaceChildren(
    el("span", {}, note.folder),
    el("span", {}, `Edited ${fmtDate(note.modified_ms)}`),
    el("span", {}, `Created ${fmtDate(note.created_ms)}`),
    ...note.tags.map((t) => el("button", { class: "chip", onclick: () => addFilter(`tag:${t}`) }, `#${t}`)),
  );
  renderAttachmentList(note);
  $("#reader").scrollTop = 0;
}

function markText(root) {
  if (!state.terms.length) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    if (state.terms.some((t) => n.nodeValue.toLowerCase().includes(t))) n.replaceWith(highlightText(n.nodeValue));
  }
  root.querySelector("mark")?.scrollIntoView({ block: "center" });
}

function renderAttachmentList(note) {
  const box = $("#reader-atts");
  const files = note.attachments.filter((a) => a.kind !== "table");
  box.hidden = !files.length;
  box.replaceChildren(
    el("h3", {}, "Attachments"),
    el("ul", {}, files.map((a) =>
      el("li", {},
        el("span", {}, a.filename ?? a.id),
        el("span", { class: "muted" }, attStatusText(a)),
        a.file ? el("button", { onclick: () => downloadAttachment(a.id) }, "Download") : null,
      ))),
  );
}

function attStatusText(a) {
  const size = a.size ? ` ${(a.size / 1024 / 1024).toFixed(a.size > 1048576 ? 1 : 2)} MB` : "";
  return {
    ok: size,
    preview: ` preview only${size}`,
    missing: " not found in the source",
    too_large: " skipped: larger than the export limit",
    skipped: " not exported",
  }[a.status] ?? "";
}

async function attachmentBlob(id) {
  const a = state.atts.get(id);
  if (!a?.file) return null;
  const f = state.files.get(a.file.split("/").pop());
  if (!f) return { missingFile: true };
  const bytes = new Uint8Array(await f.arrayBuffer());
  const data = state.key ? await decrypt(state.key, bytes, `att:${id}`) : bytes;
  return new Blob([data], { type: a.mime || "application/octet-stream" });
}

async function attachmentUrl(id) {
  if (state.blobUrls.has(id)) return state.blobUrls.get(id);
  const blob = await attachmentBlob(id);
  if (!blob || blob.missingFile) return null;
  const url = URL.createObjectURL(blob);
  state.blobUrls.set(id, url);
  return url;
}

async function hydrateImage(img) {
  const id = img.getAttribute("src").slice(5);
  img.removeAttribute("src");
  img.alt ||= "attachment";
  img.loading = "lazy";
  const url = await attachmentUrl(id);
  if (!url) {
    img.replaceWith(el("p", { class: "att-missing" }, `${img.alt}: file not loaded. Select the export folder, including files/.`));
    return;
  }
  img.addEventListener("error", () => {
    img.replaceWith(el("p", { class: "att-missing" }, `${img.alt}: this browser cannot show this image format. `,
      el("button", { onclick: () => downloadAttachment(id) }, "Download")));
  }, { once: true });
  img.src = url;
}

async function openAttachment(id) {
  const url = await attachmentUrl(id);
  if (!url) { toast("Attachment file not loaded. Select the export folder, including files/."); return; }
  const a = state.atts.get(id);
  if (/^(application\/pdf|image\/|video\/|audio\/|text\/plain)/.test(a?.mime ?? "")) window.open(url, "_blank", "noopener");
  else downloadAttachment(id);
}

async function downloadAttachment(id) {
  const url = await attachmentUrl(id);
  if (!url) { toast("Attachment file not loaded."); return; }
  saveUrl(url, state.atts.get(id)?.filename ?? id);
}

function saveUrl(url, name) {
  const a = el("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
}

function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  saveUrl(url, name);
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

// ---------- markdown export ----------

const safeName = (s, fallback = "untitled") =>
  (s || fallback).replace(/[\\/:*?"<>|\u0000-\u001f]/g, "-").replace(/\s+/g, " ").trim().slice(0, 100) || fallback;

const yamlStr = (s) => JSON.stringify(s ?? "");

function noteMarkdown(note, attPath) {
  const front = [
    "---",
    `title: ${yamlStr(note.title)}`,
    `folder: ${yamlStr(note.folder)}`,
    `created: ${note.created_ms ? new Date(note.created_ms).toISOString() : ""}`,
    `modified: ${note.modified_ms ? new Date(note.modified_ms).toISOString() : ""}`,
    `tags: [${note.tags.map(yamlStr).join(", ")}]`,
    "source: Apple Notes",
    "---",
    "",
  ].join("\n");
  const body = note.body_md.replace(/\]\(att:([\w-]+)\)/g, (_, id) => `](${encodeURI(attPath(id))})`);
  return front + body;
}

function attachmentNames(note) {
  const used = new Set();
  const names = new Map();
  for (const a of note.attachments) {
    let name = safeName(a.filename, a.id);
    for (let i = 2; used.has(name); i++) name = name.replace(/(\.[^.]*)?$/, `-${i}$1`);
    used.add(name);
    names.set(a.id, name);
  }
  return names;
}

async function exportNoteZip(note, prefix = "") {
  const names = attachmentNames(note);
  const out = {};
  const base = safeName(note.title);
  out[`${prefix}${base}.md`] = strToU8(noteMarkdown(note, (id) => `attachments/${names.get(id) ?? id}`));
  let missing = 0;
  for (const a of note.attachments) {
    if (!a.file) continue;
    const blob = await attachmentBlob(a.id);
    if (!blob || blob.missingFile) { missing++; continue; }
    out[`${prefix}attachments/${names.get(a.id)}`] = new Uint8Array(await blob.arrayBuffer());
  }
  return { out, missing };
}

async function copyMarkdown() {
  const n = state.current;
  if (!n) return;
  await navigator.clipboard.writeText(noteMarkdown(n, (id) => state.atts.get(id)?.filename ?? id));
  toast("Markdown copied");
}

function downloadMarkdown() {
  const n = state.current;
  if (!n) return;
  const names = attachmentNames(n);
  const md = noteMarkdown(n, (id) => `attachments/${names.get(id) ?? id}`);
  saveBlob(new Blob([md], { type: "text/markdown" }), `${safeName(n.title)}.md`);
}

async function downloadZip() {
  const n = state.current;
  if (!n) return;
  const { out, missing } = await exportNoteZip(n);
  saveBlob(new Blob([zipSync(out, { level: 6 })], { type: "application/zip" }), `${safeName(n.title)}.zip`);
  if (missing) toast(`${missing} attachment file${missing > 1 ? "s were" : " was"} not loaded and left out`);
}

async function exportResults() {
  if (!state.results.length) return;
  const btn = $("#export-results");
  btn.disabled = true;
  try {
    const out = {};
    let missing = 0;
    const usedDirs = new Set();
    for (const r of state.results) {
      const [note] = await rows(SQL.note, [r.id]);
      note.tags = note.tags_s ? note.tags_s.split(SEP) : [];
      note.attachments = [...state.atts.values()].filter((a) => a.note_id === r.id);
      let dir = `${note.folder.split("/").map((p) => safeName(p)).join("/")}/`;
      // Each note gets its own attachments folder only when needed.
      if (note.attachments.length) {
        let d = `${dir}${safeName(note.title)}/`;
        for (let i = 2; usedDirs.has(d); i++) d = `${dir}${safeName(note.title)} ${i}/`;
        usedDirs.add(d);
        dir = d;
      }
      const res = await exportNoteZip(note, dir);
      for (const [k, v] of Object.entries(res.out)) {
        let key = k;
        for (let i = 2; key in out; i++) key = k.replace(/\.md$/, ` ${i}.md`);
        out[key] = v;
      }
      missing += res.missing;
    }
    saveBlob(new Blob([zipSync(out, { level: 6 })], { type: "application/zip" }), "notes-export.zip");
    toast(`Exported ${state.results.length} notes${missing ? `, ${missing} attachment files not loaded` : ""}`);
  } finally {
    btn.disabled = false;
  }
}

// ---------- sidebar ----------

async function renderSidebar() {
  const folders = await rows(SQL.folders);
  const tree = new Map();
  for (const { folder, n } of folders) {
    const parts = folder.split("/");
    for (let i = 1; i <= parts.length; i++) {
      const path = parts.slice(0, i).join("/");
      const node = tree.get(path) ?? { path, name: parts[i - 1], depth: i - 1, n: 0 };
      node.n += n;
      tree.set(path, node);
    }
  }
  const nodes = [...tree.values()].sort((a, b) => a.path.localeCompare(b.path));
  $("#folders").replaceChildren(
    el("li", {}, el("button", { onclick: () => setQuery("") }, "All notes")),
    ...nodes.map((f) =>
      el("li", { style: `--depth:${f.depth}` },
        el("button", { onclick: () => addFilter(`folder:"${f.path}"`, /folder:("[^"]*"|\S+)/g) },
          el("span", {}, f.name), el("span", { class: "n" }, String(f.n))))),
  );
  const tags = await rows(SQL.tags);
  $("#tags-box").hidden = !tags.length;
  $("#tags").replaceChildren(...tags.map((t) =>
    el("li", {}, el("button", { class: "chip", onclick: () => addFilter(`tag:${t.tag}`) }, `#${t.tag}`,
      el("span", { class: "n" }, String(t.n))))));
}

function setQuery(v) {
  $("#q").value = v;
  state.selected = -1;
  search();
  document.body.dataset.pane = "list";
  $("#rail").classList.remove("open");
}

function addFilter(token, replacePattern) {
  let v = $("#q").value;
  if (replacePattern) v = v.replace(replacePattern, "");
  if (!v.includes(token)) v = `${v.trim()} ${token}`;
  setQuery(v.trim() + " ");
  $("#q").focus();
}

// ---------- SQL console ----------

async function runConsole(ev) {
  ev.preventDefault();
  const sql = $("#sql").value.trim();
  const out = $("#sql-out");
  if (!sql) return;
  try {
    const table = await state.conn.query(sql);
    const cols = table.schema.fields.map((f) => f.name);
    const data = table.toArray().slice(0, 200).map((r) => r.toJSON());
    out.replaceChildren(
      el("p", { class: "muted" }, `${table.numRows} rows${table.numRows > 200 ? ", first 200 shown" : ""}`),
      el("div", { class: "scroll" }, el("table", {},
        el("thead", {}, el("tr", {}, cols.map((c) => el("th", {}, c)))),
        el("tbody", {}, data.map((r) => el("tr", {}, cols.map((c) => el("td", {}, cellText(r[c]))))))))
    );
  } catch (err) {
    out.replaceChildren(el("p", { class: "error" }, err.message));
  }
}

function cellText(v) {
  if (v == null) return "";
  if (typeof v === "bigint") return v.toString();
  if (typeof v === "object") return v.toString?.() ?? JSON.stringify(v);
  const s = String(v);
  return s.length > 300 ? `${s.slice(0, 300)}…` : s;
}

// ---------- misc ----------

let toastTimer = 0;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3500);
}

function debounce(fn, ms) {
  let t = 0;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function onKey(e) {
  armIdleLock();
  if (document.body.dataset.view !== "app") return;
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
  if (e.key === "/" && !typing) { e.preventDefault(); $("#q").focus(); $("#q").select(); return; }
  if (e.key === "Escape") {
    if (document.body.dataset.pane === "reader") document.body.dataset.pane = "list";
    else if (typing) document.activeElement.blur();
    return;
  }
  if (typing && document.activeElement !== $("#q")) return;
  if (e.key === "ArrowDown" || (!typing && e.key === "j")) { e.preventDefault(); select(state.selected + 1); }
  if (e.key === "ArrowUp" || (!typing && e.key === "k")) { e.preventDefault(); select(Math.max(0, state.selected - 1)); }
  if (e.key === "Enter" && typing && state.selected < 0) select(0);
}

function init() {
  for (const [v, label] of Object.entries(SORTS)) $("#sort").append(el("option", { value: v }, label));
  const drop = $("#drop");
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", async (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    await addFiles(await collectEntries([...e.dataTransfer.items]));
  });
  $("#pick-folder").addEventListener("change", (e) => addFiles([...e.target.files]));
  $("#pick-files").addEventListener("change", (e) => addFiles([...e.target.files]));
  $("#unlock-form").addEventListener("submit", unlock);
  $("#q").addEventListener("input", debounce(() => { state.selected = -1; search(); }, 120));
  $("#sort").addEventListener("change", search);
  $("#lock").addEventListener("click", lock);
  $("#menu").addEventListener("click", () => $("#rail").classList.toggle("open"));
  $("#back").addEventListener("click", () => (document.body.dataset.pane = "list"));
  $("#copy-md").addEventListener("click", copyMarkdown);
  $("#dl-md").addEventListener("click", downloadMarkdown);
  $("#dl-zip").addEventListener("click", downloadZip);
  $("#export-results").addEventListener("click", exportResults);
  $("#sql-form").addEventListener("submit", runConsole);
  document.addEventListener("keydown", onKey);
  document.addEventListener("pointerdown", armIdleLock);
  if (!("webkitdirectory" in HTMLInputElement.prototype)) $("#pick-folder-label").hidden = true;
}

init();
