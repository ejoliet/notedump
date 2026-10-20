import gzip
import json
import sqlite3

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from notedump import crypto
from notedump.export import ExportError, Options, run
from notedump.sources import from_container

from .conftest import FIXTURE, PASS


def _decrypt_to(out, name, dst):
    """Decrypt <name>.arrows.gz.enc and write it as Parquet for easy querying."""
    m = json.loads((out / "notedump.json").read_text())
    key = crypto.derive_key(PASS, crypto.b64d(m["kdf"]["salt"]), m["kdf"]["iterations"])
    data = crypto.decrypt(key, (out / f"{name}.arrows.gz.enc").read_bytes(), f"{name}.arrows.gz")
    tbl = pa.ipc.open_stream(gzip.decompress(data)).read_all()
    pq.write_table(tbl, dst)
    return key


def test_encrypted_export_roundtrip(export_dir, tmp_path):
    out, report = export_dir
    assert report.notes == 7 and report.locked_skipped == 1 and report.deleted_skipped == 1
    manifest = (out / "notedump.json").read_text()
    assert "bitcoin" not in manifest and "Folder2" not in manifest
    for f in out.rglob("*"):
        if f.is_file() and f.name != "notedump.json":
            assert f.read_bytes()[:4] == crypto.MAGIC, f
            assert b"bitcoin" not in f.read_bytes()

    key = _decrypt_to(out, "notes", tmp_path / "n.parquet")
    _decrypt_to(out, "attachments", tmp_path / "a.parquet")
    con = duckdb.connect()
    rows = dict(con.execute(f"SELECT title, folder FROM '{tmp_path}/n.parquet'").fetchall())
    assert rows["This is a deeply buried note"] == "Folder2/Subfolder/Subsubfolder"
    tags = con.execute(
        f"SELECT tags FROM '{tmp_path}/n.parquet' WHERE title = 'This note has tags'"
    ).fetchone()[0]
    assert tags == ["travel", "vacation"]
    md = con.execute(
        f"SELECT body_md FROM '{tmp_path}/n.parquet' WHERE n_attachments = 1 AND title LIKE '%attachment%'"
    ).fetchone()[0]
    assert "](att:D3C05C99-38DB-448B-B687-AB4F63ADF7DB)" in md

    att = con.execute(
        f"SELECT id, file, status, size FROM '{tmp_path}/a.parquet' WHERE kind = 'file'"
    ).fetchone()
    assert att[2] == "ok" and att[3] == 184292
    pdf = crypto.decrypt(key, (out / att[1]).read_bytes(), f"att:{att[0]}")
    assert pdf[:5] == b"%PDF-"
    # AAD binds each blob to its name: swapping files must fail.
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(key, (out / att[1]).read_bytes(), "att:other")


def test_reexport_is_incremental_and_checks_passphrase(export_dir, fast_kdf):
    out, _ = export_dir
    src = from_container(FIXTURE)
    try:
        again = run(src, Options(out=out, passphrase=PASS))
        assert again.files_written == 0 and again.files_reused == 1
        with pytest.raises(ExportError, match="does not match"):
            run(src, Options(out=out, passphrase="a different passphrase"))
        rekeyed = run(src, Options(out=out, passphrase="a different passphrase", rekey=True))
        assert rekeyed.files_written == 1
    finally:
        src.close()


def test_plain_and_sqlite(tmp_path):
    src = from_container(FIXTURE)
    try:
        run(
            src,
            Options(
                out=tmp_path / "plain",
                passphrase=None,
                include_deleted=True,
                dump_dir=tmp_path / "dump",
            ),
        )
    finally:
        src.close()
    assert duckdb.sql(f"SELECT count(*) FROM '{tmp_path}/dump/notes.parquet'").fetchone()[0] == 8
    con = sqlite3.connect(tmp_path / "dump" / "notes.sqlite")
    assert con.execute("SELECT count(*) FROM notes").fetchone()[0] == 8
    assert con.execute("SELECT count(*) FROM notes WHERE deleted").fetchone()[0] == 1
    assert json.loads(
        con.execute("SELECT tags FROM notes WHERE title='This note has tags'").fetchone()[0]
    ) == ["travel", "vacation"]


def test_attachment_pruned_when_gone(container_copy, tmp_path, fast_kdf):
    out = tmp_path / "e"
    src = from_container(container_copy)
    run(src, Options(out=out, passphrase=PASS))
    src.close()
    assert len(list((out / "files").iterdir())) == 1
    src = from_container(container_copy)
    report = run(src, Options(out=out, passphrase=PASS, attachments=False))
    src.close()
    assert report.files_pruned == 1 and not list((out / "files").iterdir())
