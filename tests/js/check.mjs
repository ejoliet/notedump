// Usage: node check.mjs <export_dir> <passphrase>
// Runs web/crypto.js and web/query.js exactly as the viewer does, on DuckDB-WASM.
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { deriveKey, decrypt, verifyPassphrase } from "../../web/crypto.js";
import { parseQuery, buildSql, SQL } from "../../web/query.js";

const require = createRequire(import.meta.url);
const duckdb = require("@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs");
const DIST = path.dirname(require.resolve("@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs"));

const [dir, pass] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(path.join(dir, "notedump.json"), "utf8"));
const out = {};

const key = await deriveKey(pass, manifest);
out.passOk = await verifyPassphrase(key, manifest);
out.wrongPassOk = await verifyPassphrase(await deriveKey(pass + "x", manifest), manifest);
const gunzip = async (u8) => new Uint8Array(
  await new Response(new Blob([u8]).stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer());
const notes = await gunzip(await decrypt(key, readFileSync(path.join(dir, "notes.arrows.gz.enc")), "notes.arrows.gz"));
const atts = await gunzip(await decrypt(key, readFileSync(path.join(dir, "attachments.arrows.gz.enc")), "attachments.arrows.gz"));

const db = await duckdb.createDuckDB(
  {
    mvp: { mainModule: path.join(DIST, "duckdb-mvp.wasm"), mainWorker: path.join(DIST, "duckdb-node-mvp.worker.cjs") },
    eh: { mainModule: path.join(DIST, "duckdb-eh.wasm"), mainWorker: path.join(DIST, "duckdb-node-eh.worker.cjs") },
  },
  new duckdb.VoidLogger(),
  duckdb.NODE_RUNTIME,
);
await db.instantiate(() => {});
const conn = db.connect();
const rows = (sql, params = []) => {
  const t = params.length ? conn.prepare(sql).query(...params) : conn.query(sql);
  return t.toArray().map((r) => r.toJSON());
};

conn.insertArrowFromIPCStream(notes, { name: "notes_raw", create: true });
conn.insertArrowFromIPCStream(atts, { name: "attachments", create: true });
conn.query(SQL.createNotes);

out.attachments = rows(SQL.attachments);
out.folders = rows(SQL.folders);
out.tags = rows(SQL.tags);
out.searches = {};
for (const q of ["", "bitcoin", "\"in a subfolder\"", "-note", "tag:travel", "folder:Folder2",
  "has:file", "is:deleted", "after:2025-08-02", "before:2025-07-31 note"]) {
  const { where, order, params, orderParams } = buildSql(parseQuery(q), "best");
  const [{ n }] = rows(SQL.count(where), params);
  const page = rows(SQL.page(where, order, 300), [...params, ...orderParams]);
  out.searches[q] = { n, titles: page.map((r) => r.title), first: page[0] ?? null };
}
const id = out.searches["bitcoin"].first.id;
const [note] = rows(SQL.note, [id]);
out.note = { id: note.id, typeofId: typeof note.id, typeofModified: typeof note.modified_ms,
  tags_s: note.tags_s, hasAttLink: note.body_md.includes("](att:") };
console.log(JSON.stringify(out));
