// Search box syntax -> parameterized DuckDB SQL. Pure functions, tested in Node.
//
//   word "exact phrase" -excluded
//   tag:travel  folder:"Work/Projects"  is:pinned  is:deleted
//   has:attachment  has:image  has:link  after:2024-01-01  before:2025-06-30

const DATE = /^\d{4}-\d{2}-\d{2}$/;

export function tokenize(input) {
  const out = [];
  const re = /(-?)(?:(\w+):)?(?:"([^"]*)"|(\S+))/g;
  let m;
  while ((m = re.exec(input)) !== null) {
    const value = (m[3] ?? m[4] ?? "").trim();
    if (value) out.push({ neg: m[1] === "-", op: m[2]?.toLowerCase() ?? null, value });
  }
  return out;
}

export function parseQuery(input) {
  const q = { terms: [], exclude: [], tags: [], folders: [], is: new Set(), has: new Set(),
    after: null, before: null };
  for (const t of tokenize(input ?? "")) {
    const v = t.value;
    switch (t.op) {
      case "tag": q.tags.push(v.replace(/^#/, "").toLowerCase()); break;
      case "folder": case "in": q.folders.push(v); break;
      case "is": q.is.add(v.toLowerCase()); break;
      case "has": q.has.add(v.toLowerCase()); break;
      case "after": if (DATE.test(v)) q.after = v; break;
      case "before": if (DATE.test(v)) q.before = v; break;
      default: {
        // Unknown "x:y" stays a literal search term (URLs, times like 10:30).
        const text = (t.op ? `${t.op}:${v}` : v).toLowerCase();
        (t.neg ? q.exclude : q.terms).push(text);
      }
    }
  }
  return q;
}

export const SORTS = {
  best: "Best match",
  modified: "Recently edited",
  created: "Recently created",
  oldest: "Oldest first",
  title: "Title",
};

/**
 * Build WHERE/ORDER for the `notes` table (expects a lowercase `hay` column).
 * Returns { where, order, params, orderParams } so callers can count and page.
 */
export function buildSql(q, sort = "best") {
  const where = [];
  const params = [];
  for (const t of q.terms) { where.push("contains(hay, ?)"); params.push(t); }
  for (const t of q.exclude) { where.push("NOT contains(hay, ?)"); params.push(t); }
  for (const tag of q.tags) {
    where.push("list_contains(list_transform(tags, x -> lower(x)), ?)");
    params.push(tag);
  }
  if (q.folders.length) {
    const ors = q.folders.map(() => "(folder = ? OR starts_with(folder, ? || '/'))");
    where.push(`(${ors.join(" OR ")})`);
    for (const f of q.folders) params.push(f, f);
  }
  if (q.is.has("pinned")) where.push("pinned");
  where.push(q.is.has("deleted") ? "deleted" : "NOT deleted"); // is:deleted shows only those
  if (q.has.has("attachment") || q.has.has("attachments")) where.push("n_attachments > 0");
  if (q.has.has("link") || q.has.has("links")) where.push("len(links) > 0");
  for (const kind of ["image", "file", "video", "audio", "drawing", "scan"]) {
    if (q.has.has(kind)) {
      where.push("id IN (SELECT note_id FROM attachments WHERE kind = ?)");
      params.push(kind);
    }
  }
  // AIDEV-NOTE: no TIMESTAMPTZ math. It needs the ICU extension, which DuckDB-WASM
  // would fetch from the network. Compare epoch ms; dates are UTC days.
  if (q.after) { where.push("epoch_ms(modified) >= epoch_ms(CAST(? AS TIMESTAMP))"); params.push(q.after); }
  if (q.before) {
    where.push("epoch_ms(modified) < epoch_ms(CAST(? AS TIMESTAMP) + INTERVAL 1 DAY)");
    params.push(q.before);
  }

  const orderParams = [];
  let order;
  if (sort === "best" && q.terms.length) {
    // Title hits first, then more term hits in the title, then recency.
    const score = q.terms.map(() => "CASE WHEN contains(lower(title), ?) THEN 1 ELSE 0 END");
    orderParams.push(...q.terms);
    order = `pinned DESC, (${score.join(" + ")}) DESC, modified DESC`;
  } else if (sort === "created") order = "created DESC";
  else if (sort === "oldest") order = "modified ASC";
  else if (sort === "title") order = "lower(title) ASC";
  else order = "pinned DESC, modified DESC";

  return { where: where.length ? where.join(" AND ") : "TRUE", order, params, orderParams };
}

/** Terms worth highlighting (no operators, no exclusions). */
export function highlightTerms(q) {
  return [...new Set(q.terms)].filter((t) => t.length > 0).sort((a, b) => b.length - a.length);
}

// ---------- SQL used by app.js (kept here so tests run the exact statements) ----------

export const SEP = "\u001f";

// JS-friendly types: no BigInt ids, ms timestamps as numbers, tags as one string.
export const NOTE_COLS = `CAST(id AS INTEGER) AS id, title, folder, pinned, deleted,
  CAST(n_attachments AS INTEGER) AS n_att,
  CAST(epoch_ms(modified) AS DOUBLE) AS modified_ms,
  CAST(epoch_ms(created) AS DOUBLE) AS created_ms,
  array_to_string(tags, '${SEP}') AS tags_s`;

export const SQL = {
  // Raw Arrow lands in notes_raw; `hay` is the lowercase search haystack.
  createNotes: `CREATE OR REPLACE TABLE notes AS
    SELECT *, lower(coalesce(title, '') || chr(10) || coalesce(body_text, '')) AS hay FROM notes_raw;
    DROP TABLE notes_raw`,
  emptyAttachments: `CREATE OR REPLACE TABLE attachments (id VARCHAR, note_id BIGINT, kind VARCHAR,
    uti VARCHAR, mime VARCHAR, filename VARCHAR, size BIGINT, file VARCHAR, status VARCHAR)`,
  attachments: `SELECT id, CAST(note_id AS INTEGER) AS note_id, kind, mime, filename,
    CAST(size AS DOUBLE) AS size, file, status FROM attachments`,
  folders: "SELECT folder, CAST(count(*) AS INTEGER) AS n FROM notes WHERE NOT deleted GROUP BY folder",
  tags: `SELECT t AS tag, CAST(count(*) AS INTEGER) AS n
    FROM (SELECT unnest(tags) AS t FROM notes WHERE NOT deleted) GROUP BY t ORDER BY n DESC, t LIMIT 200`,
  count: (where) => `SELECT CAST(count(*) AS INTEGER) AS n FROM notes WHERE ${where}`,
  page: (where, order, limit) =>
    `SELECT ${NOTE_COLS}, body_text FROM notes WHERE ${where} ORDER BY ${order} LIMIT ${limit}`,
  note: `SELECT ${NOTE_COLS}, body_md, uuid, account FROM notes WHERE id = ?`,
};
