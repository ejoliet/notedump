# notedump

Export Apple Notes into an encrypted folder you can keep in Box or Dropbox, then search and read it from a static web page. Decryption and search run in the browser tab. Nothing is uploaded.

- **Source:** this Mac's Notes, or an unencrypted iPhone backup.
- **Output:** encrypted Arrow tables plus encrypted attachment files.
- **Viewer:** a static page using DuckDB-WASM for search and filtering, with Markdown export.

## Quick start

```bash
# Mac Notes (grant your terminal Full Disk Access first)
uvx --from git+https://github.com/ejoliet/notedump notedump local -o ~/Dropbox/notes-export

# iPhone backup (newest backup in ~/Library/Application Support/MobileSync/Backup)
uvx --from git+https://github.com/ejoliet/notedump notedump backup -o ~/Dropbox/notes-export
```

It asks for a passphrase twice on the first export (12 characters minimum). Later exports into the same folder ask once and must use the same passphrase.

Open the viewer at `https://ejoliet.github.io/notedump/` (after Pages is enabled), or locally:

```bash
python3 -m http.server -d web 8000   # then open http://localhost:8000
```

Drop the export folder on the page and enter the passphrase. On iPhone, tap **Choose files** and select `notedump.json`, `notes.arrows.gz.enc`, `attachments.arrows.gz.enc`, plus any files from `files/` you want to see.

## CLI options

| Option | Effect |
|---|---|
| `-o DIR` | Export folder (required) |
| `--dump DIR` | Also write **plaintext** `notes.parquet`, `attachments.parquet`, `notes.sqlite` |
| `--include-deleted` | Include Recently Deleted (viewer: `is:deleted`) |
| `--no-attachments` | Metadata only, no files |
| `--max-attachment-mb N` | Skip larger files (default 100) |
| `--rekey` | New passphrase and salt; rewrites everything |
| `--plain` | No encryption (debugging only) |
| `--container DIR` | (`local`) Use another Notes group container |

`NOTEDUMP_PASSPHRASE` supplies the passphrase non-interactively (scripts, cron). Avoid it in shell history.

## Viewer search syntax

`word` `"exact phrase"` `-excluded` `tag:travel` `folder:"Work/Projects"` `is:pinned` `is:deleted` `has:attachment` `has:image` `has:link` `after:2024-01-01` `before:2024-12-31`

Keys: `/` search, `j`/`k` or arrows to move, `Esc` back. Each note can be copied as Markdown, downloaded as `.md`, or downloaded as `.zip` with attachments. **Export results** zips every note in the current result list. A SQL console queries the `notes` and `attachments` tables directly.

## Security model

- **Encryption:** AES-256-GCM with a key derived from your passphrase (PBKDF2-HMAC-SHA256, 600,000 iterations). Each file is bound to its logical name, so files cannot be swapped.
- **Manifest:** `notedump.json` holds only the KDF parameters, a passphrase check, and the export time. It contains no titles, counts, or folder names.
- **File names:** attachment files have keyed opaque names, so they reveal nothing.
- **In the browser:** the key is a non-extractable WebCrypto key held in memory. Nothing is written to browser storage. **Lock** or 15 minutes idle clears all decrypted data.
- **Network:** the page's CSP allows network access only to `cdn.jsdelivr.net` (pinned library versions) and Google Fonts. Decrypted notes cannot be sent anywhere else.
- **Your responsibility:** the passphrase is the only protection for files in a synced folder. Use a long one; there is no recovery.

## What is not exported (v0.1)

- **Locked notes:** skipped and counted. Their content is encrypted by Apple.
- **Tables:** shown as a placeholder.
- **Drawings and scans:** exported as their preview image when one exists.
- **Encrypted iPhone backups:** refused with instructions. Use `notedump local` on a Mac that syncs Notes via iCloud, or make an unencrypted backup.
- **HEIC images:** show inline in Safari only. Other browsers get a download button.

## Development

```bash
uv sync
uv run pytest                                  # Python tests
npm install --prefix tests/js && uv run pytest # adds the DuckDB-WASM viewer test
```

`tests/test_web_js.py` runs the viewer's own `crypto.js` and `query.js` against a real export on DuckDB-WASM in Node.

Test fixture: `tests/fixtures/group.com.apple.notes` comes from [apple-notes-parser](https://github.com/RhetTbull/apple-notes-parser) (MIT).

## License

MIT
