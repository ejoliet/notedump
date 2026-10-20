# AGENTS.md — notedump

## Invariants (load-bearing)

| Rule | Why | Blast radius |
|---|---|---|
| `src/notedump/crypto.py` and `web/crypto.js` implement one format. Change both, bump `FORMAT_VERSION`. | Exports must open in the viewer. | Every existing export becomes unreadable. |
| Viewer payload is gzipped Arrow IPC, not Parquet. | DuckDB-WASM 1.32 lacks built-in Parquet; it fetches the extension from extensions.duckdb.org, which the CSP blocks. | Viewer fails to load in browsers. |
| No `TIMESTAMPTZ` arithmetic or casts in viewer SQL; use `epoch_ms`. | Needs the ICU extension; same network fetch problem. | Date filters throw in the browser. |
| Viewer SQL lives in `web/query.js` (`SQL`, `NOTE_COLS`, `buildSql`). | `tests/js/check.mjs` runs that exact SQL on DuckDB-WASM. | SQL inlined in `app.js` is untested. |
| CSP `connect-src` stays `https://cdn.jsdelivr.net` only. | It is the guarantee that decrypted notes never leave the tab. | Privacy promise in README breaks. |
| Never write passphrases, keys, or decrypted data to browser storage. | Synced-folder threat model; tab isolation. | Plaintext persists on shared machines. |
| `notedump.json` stays content-free. | It sits unencrypted in a synced folder. | Leaks note metadata. |
| Re-exports reuse the salt and keyed attachment names. | Sync clients only upload changes. | Full re-upload of every attachment on each run. |
| Run lengths in Notes protobuf are UTF-16 code units. | `markdown._split_utf16`. | Formatting shifts after any emoji. |

## Layout

- `src/notedump/sources.py`: finds NoteStore and media (container or backup `Manifest.db`).
- `src/notedump/markdown.py`: protobuf to Markdown and plain text.
- `src/notedump/export.py`: tables, attachments, encryption, `--dump`.
- `web/`: static viewer (`index.html`, `app.js`, `crypto.js`, `query.js`). No build step.

## Commands

```bash
uv run pytest
npm install --prefix tests/js   # enables tests/test_web_js.py
uv run --with ruff ruff check src tests
```

Visual review of `web/` is done by a human: `python3 -m http.server -d web 8000`.

## Recently burned

- 2026-10-20: Parquet then TIMESTAMPTZ math both broke DuckDB-WASM silently (extension autoload over blocked network). Test new SQL in `tests/js/check.mjs` first.
- 2026-10-20: Attachment uuid differs from media folder uuid. Resolve media via `ZMEDIA` -> media row `ZIDENTIFIER`.
