"""Export a Source into an (optionally encrypted) folder the web viewer can open.

Output layout (encrypted):
    notedump.json              manifest: KDF params + passphrase check, no content
    notes.arrows.gz.enc        Arrow IPC stream, gzipped, then encrypted
    attachments.arrows.gz.enc
    files/<opaque>.enc         one per exported attachment file

AIDEV-NOTE: the viewer payload is Arrow IPC, not Parquet. DuckDB-WASM 1.32 does
not bundle the Parquet extension; it would fetch it from extensions.duckdb.org,
which the viewer CSP blocks. Arrow IPC loads natively. Parquet and SQLite are
offered as an explicit plaintext --dump for other tools.

AIDEV-NOTE: re-exports into the same folder reuse the salt, so attachment
names stay stable and synced folders (Box, Dropbox) only upload what changed.
"""

from __future__ import annotations

import gzip
import json
import logging
import mimetypes
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from apple_notes_parser import AppleNotesParser

from . import crypto
from .markdown import parse_zdata, render
from .sources import Source

log = logging.getLogger(__name__)

MANIFEST = "notedump.json"
DELETED_FOLDER = "Recently Deleted"

NOTES_SCHEMA = pa.schema(
    [
        ("id", pa.int64()),
        ("uuid", pa.string()),
        ("title", pa.string()),
        ("folder", pa.string()),
        ("account", pa.string()),
        ("created", pa.timestamp("us", tz="UTC")),
        ("modified", pa.timestamp("us", tz="UTC")),
        ("pinned", pa.bool_()),
        ("deleted", pa.bool_()),
        ("tags", pa.list_(pa.string())),
        ("mentions", pa.list_(pa.string())),
        ("links", pa.list_(pa.string())),
        ("n_attachments", pa.int32()),
        ("body_text", pa.string()),
        ("body_md", pa.string()),
    ]
)
ATT_SCHEMA = pa.schema(
    [
        ("id", pa.string()),  # attachment identifier, referenced as att:<id> in body_md
        ("note_id", pa.int64()),
        ("kind", pa.string()),
        ("uti", pa.string()),
        ("mime", pa.string()),
        ("filename", pa.string()),
        ("size", pa.int64()),
        ("file", pa.string()),  # path relative to export root, null if not exported
        ("status", pa.string()),  # ok | preview | missing | too_large | inline | skipped
    ]
)

INLINE_UTIS = {
    "com.apple.notes.inlinetextattachment.hashtag",
    "com.apple.notes.inlinetextattachment.mention",
    "com.apple.notes.inlinetextattachment.link",
    "com.apple.notes.inlinetextattachment.calculateresult",
    "com.apple.notes.inlinetextattachment.calculategraphexpression",
}
UTI_MIME = {
    "public.jpeg": "image/jpeg",
    "public.png": "image/png",
    "public.heic": "image/heic",
    "public.tiff": "image/tiff",
    "com.compuserve.gif": "image/gif",
    "com.adobe.pdf": "application/pdf",
    "public.mpeg-4": "video/mp4",
    "com.apple.quicktime-movie": "video/quicktime",
    "com.apple.m4a-audio": "audio/mp4",
    "public.mp3": "audio/mpeg",
}


@dataclass
class Options:
    out: Path
    passphrase: str | None  # None means --plain
    include_deleted: bool = False
    attachments: bool = True
    max_attachment_bytes: int = 100 * 1024 * 1024
    dump_dir: Path | None = None  # plaintext Parquet + SQLite copy
    rekey: bool = False


@dataclass
class Report:
    notes: int = 0
    locked_skipped: int = 0
    deleted_skipped: int = 0
    unreadable: int = 0
    attachments: int = 0
    files_written: int = 0
    files_reused: int = 0
    files_pruned: int = 0
    by_status: dict[str, int] = field(default_factory=dict)


class ExportError(Exception):
    pass


def _escape_md(text: str) -> str:
    return "".join("\\" + c if c in "\\`*_[]<>" else c for c in text)


def _utc(dt: datetime | None) -> datetime | None:
    # apple_notes_parser returns naive local time; astimezone() treats naive as local.
    return dt.astimezone(UTC) if dt is not None else None


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _load_objects(con: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Every ZICCLOUDSYNCINGOBJECT row with an identifier, plus its media row."""
    have = _columns(con, "ZICCLOUDSYNCINGOBJECT")
    want = ["ZTYPEUTI", "ZTYPEUTI1", "ZFILENAME", "ZTITLE", "ZALTTEXT", "ZURLSTRING",
            "ZFILESIZE", "ZMEDIA", "ZUSERTITLE"]  # fmt: skip
    cols = [c for c in want if c in have]
    rows = con.execute(
        f"SELECT Z_PK, ZIDENTIFIER, {', '.join(cols)} FROM ZICCLOUDSYNCINGOBJECT "
        "WHERE ZIDENTIFIER IS NOT NULL"
    ).fetchall()
    by_pk: dict[int, dict[str, object]] = {}
    by_id: dict[str, dict[str, object]] = {}
    for row in rows:
        rec = dict(zip(["pk", "identifier", *cols], row, strict=True))
        by_pk[int(rec["pk"])] = rec  # type: ignore[arg-type]
        by_id[str(rec["identifier"])] = rec
    for rec in by_id.values():
        media_pk = rec.get("ZMEDIA")
        if media_pk:
            rec["media"] = by_pk.get(int(media_pk))  # type: ignore[arg-type]
    return by_id


def _kind(uti: str) -> str:
    if uti in INLINE_UTIS:
        return "inline"
    if uti == "com.apple.notes.table":
        return "table"
    if uti.startswith("com.apple.drawing") or uti == "com.apple.paper":
        return "drawing"
    if uti == "com.apple.notes.gallery":
        return "scan"
    if uti == "public.url":
        return "link"
    mime = UTI_MIME.get(uti, "")
    if mime.startswith("image/") or uti.startswith("public.") and "image" in uti:
        return "image"
    if mime.startswith(("video/", "audio/")):
        return mime.split("/")[0]
    return "file"


class _Writer:
    """Writes files into the export folder, encrypting when a key is set."""

    def __init__(self, root: Path, key: bytes | None) -> None:
        self.root, self.key = root, key

    def write(self, rel: str, data: bytes, aad: str) -> None:
        payload = crypto.encrypt(self.key, data, aad) if self.key else data
        dst = self.root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, dst)  # atomic: sync clients never see half a file

    def suffix(self) -> str:
        return ".enc" if self.key else ""


def _prepare_key(opts: Options) -> tuple[bytes | None, dict[str, object]]:
    manifest_path = opts.out / MANIFEST
    existing = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    if opts.passphrase is None:
        if existing and existing.get("encrypted"):
            raise ExportError(
                f"{opts.out} holds an encrypted export. Use another folder for --plain."
            )
        return None, {"format": "notedump", "version": crypto.FORMAT_VERSION, "encrypted": False}

    if existing and existing.get("encrypted") and not opts.rekey:
        kdf = existing["kdf"]
        key = crypto.derive_key(opts.passphrase, crypto.b64d(kdf["salt"]), int(kdf["iterations"]))
        if not crypto.verify_check(key, existing["check"]):
            raise ExportError(
                "Passphrase does not match the existing export in this folder. "
                "Use the same passphrase, a new folder, or --rekey to start over."
            )
        return key, existing
    if existing and not existing.get("encrypted"):
        raise ExportError(f"{opts.out} holds a plain export. Use another folder to encrypt.")

    salt, iterations = crypto.new_salt(), crypto.DEFAULT_ITERATIONS
    key = crypto.derive_key(opts.passphrase, salt, iterations)
    manifest = {
        "format": "notedump",
        "version": crypto.FORMAT_VERSION,
        "encrypted": True,
        "cipher": "AES-256-GCM",
        "kdf": {
            "name": "PBKDF2",
            "hash": "SHA-256",
            "iterations": iterations,
            "salt": crypto.b64e(salt),
        },
        "check": crypto.make_check(key),
    }
    return key, manifest


def run(source: Source, opts: Options) -> Report:
    """Export notes from source into opts.out. Returns counts for the CLI summary."""
    opts.out.mkdir(parents=True, exist_ok=True)
    if opts.rekey and (opts.out / "files").is_dir():
        for p in (opts.out / "files").iterdir():
            p.unlink()
    key, manifest = _prepare_key(opts)
    writer = _Writer(opts.out, key)
    report = Report()

    parser = AppleNotesParser(str(source.notestore))
    parser.load_data()
    con = sqlite3.connect(f"file:{source.notestore}?mode=ro", uri=True)
    zdata = dict(con.execute("SELECT ZNOTE, ZDATA FROM ZICNOTEDATA WHERE ZNOTE IS NOT NULL"))
    objects = _load_objects(con)
    con.close()

    note_rows: list[dict[str, object]] = []
    att_rows: dict[str, dict[str, object]] = {}
    keep_files: set[str] = set()

    for note in parser.notes:
        if note.is_password_protected:
            report.locked_skipped += 1
            continue
        folder = note.get_folder_path()
        deleted = folder.split("/")[0] == DELETED_FOLDER
        if deleted and not opts.include_deleted:
            report.deleted_skipped += 1
            continue

        seen: list[str] = []

        def render_att(
            ident: str, uti: str, _nid: int = note.note_id, seen: list[str] = seen
        ) -> tuple[str, str]:
            rec = objects.get(ident, {})
            uti = uti or str(rec.get("ZTYPEUTI") or rec.get("ZTYPEUTI1") or "")
            kind = _kind(uti)
            if kind == "inline":
                alt = str(rec.get("ZALTTEXT") or "")
                return _escape_md(alt), alt
            media = rec.get("media") or {}
            name = str(media.get("ZFILENAME") or rec.get("ZFILENAME") or rec.get("ZTITLE")
                       or rec.get("ZUSERTITLE") or kind)  # fmt: skip
            if kind == "link":
                url = str(rec.get("ZURLSTRING") or "")
                title = str(rec.get("ZTITLE") or url)
                return (f"[{title}](<{url}>)" if url else title), title
            if ident not in seen:
                seen.append(ident)
                att_rows.setdefault(ident, {"id": ident, "note_id": _nid, "kind": kind,
                                            "uti": uti, "filename": name})  # fmt: skip
            if kind == "table":
                return "*[table: not exported in this version]*", "[table]"
            label = name.replace("]", "\\]").replace("[", "\\[")
            prefix = "!" if kind in ("image", "drawing", "scan") else ""
            return f"{prefix}[{label}](att:{ident})", f"[{name}]"

        parsed = parse_zdata(zdata.get(note.note_id))
        if parsed is not None:
            body_md, body_text = render(parsed, render_att)
        else:
            report.unreadable += 1
            body_text = (note.content or "").replace("\ufffc", "")
            body_md = body_text + "\n"

        note_rows.append(
            {
                "id": note.note_id,
                "uuid": note.uuid,
                "title": note.title or (body_text.splitlines() or ["Untitled"])[0][:120],
                "folder": folder,
                "account": note.account.name,
                "created": _utc(note.creation_date),
                "modified": _utc(note.modification_date),
                "pinned": bool(note.is_pinned),
                "deleted": deleted,
                "tags": sorted(set(note.tags)),
                "mentions": list(note.mentions),
                "links": list(note.links),
                "n_attachments": len(seen),
                "body_text": body_text,
                "body_md": body_md,
            }
        )

    # Attachment files
    for ident, row in att_rows.items():
        rec = objects.get(ident, {})
        media = rec.get("media") or {}
        row["mime"] = UTI_MIME.get(str(row["uti"])) or (
            mimetypes.guess_type(str(row["filename"]))[0] if row["filename"] else None
        )
        row["size"] = rec.get("ZFILESIZE") or None
        row["file"] = None
        if row["kind"] == "table":
            row["status"] = "inline"
            continue
        if not opts.attachments:
            row["status"] = "skipped"
            continue
        candidates = source.media_dir_files(str(media["identifier"])) if media else []
        status = "ok"
        if not candidates:
            candidates, status = source.fallback_files(ident), "preview"
        if not candidates:
            row["status"] = "missing"
            continue
        src = candidates[0]
        size = src.stat().st_size
        row["size"] = size
        if status == "preview":
            row["mime"] = mimetypes.guess_type(src.name)[0] or "image/png"
            row["filename"] = f"{row['filename']} (preview){src.suffix}"
        if size > opts.max_attachment_bytes:
            row["status"] = "too_large"
            continue
        rel = f"files/{crypto.opaque_name(key, f'{ident}:{size}:{src.name}')}{writer.suffix()}"
        row["file"], row["status"] = rel, status
        keep_files.add(rel)
        if (opts.out / rel).exists():
            report.files_reused += 1
        else:
            writer.write(rel, src.read_bytes(), aad=f"att:{ident}")
            report.files_written += 1

    files_dir = opts.out / "files"
    if files_dir.is_dir():
        for p in files_dir.iterdir():
            if f"files/{p.name}" not in keep_files:
                p.unlink()
                report.files_pruned += 1

    notes_tbl = pa.Table.from_pylist(note_rows, schema=NOTES_SCHEMA)
    att_tbl = pa.Table.from_pylist(list(att_rows.values()), schema=ATT_SCHEMA)
    for name, tbl in (("notes.arrows.gz", notes_tbl), ("attachments.arrows.gz", att_tbl)):
        writer.write(name + writer.suffix(), _arrow_gz_bytes(tbl), aad=name)

    manifest["exported_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    manifest["files"] = {
        "notes": "notes.arrows.gz" + writer.suffix(),
        "attachments": "attachments.arrows.gz" + writer.suffix(),
    }
    (opts.out / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")

    if opts.dump_dir:
        opts.dump_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(notes_tbl, opts.dump_dir / "notes.parquet", compression="zstd")
        pq.write_table(att_tbl, opts.dump_dir / "attachments.parquet", compression="zstd")
        write_sqlite(opts.dump_dir / "notes.sqlite", notes_tbl, att_tbl)

    report.notes = len(note_rows)
    report.attachments = len(att_rows)
    for row in att_rows.values():
        s = str(row["status"])
        report.by_status[s] = report.by_status.get(s, 0) + 1
    return report


def _arrow_gz_bytes(tbl: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, tbl.schema) as w:
        w.write_table(tbl)
    # gzip because browsers decompress it natively (DecompressionStream).
    return gzip.compress(sink.getvalue().to_pybytes(), compresslevel=6, mtime=0)


def write_sqlite(path: Path, notes: pa.Table, atts: pa.Table) -> None:
    """Plaintext SQLite copy for other tools. List columns are stored as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    for name, tbl in (("notes", notes), ("attachments", atts)):
        cols = tbl.column_names
        con.execute(f"CREATE TABLE {name} ({', '.join(cols)})")
        rows = []
        for rec in tbl.to_pylist():
            rows.append(
                [
                    json.dumps(v) if isinstance(v, list)
                    else v.isoformat() if isinstance(v, datetime)
                    else v
                    for v in (rec[c] for c in cols)
                ]
            )  # fmt: skip
        con.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(cols))})", rows)
    con.execute("CREATE INDEX notes_modified ON notes(modified)")
    con.commit()
    con.close()
