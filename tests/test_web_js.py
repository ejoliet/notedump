"""Run the viewer's own JS (crypto.js, query.js) against a real export on DuckDB-WASM."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from .conftest import PASS

JS = Path(__file__).parent / "js"
needs_node = pytest.mark.skipif(
    shutil.which("node") is None or not (JS / "node_modules").is_dir(),
    reason="run `npm install --prefix tests/js` to enable",
)


@needs_node
def test_viewer_js_against_export(export_dir):
    out, _ = export_dir
    res = subprocess.run(["node", str(JS / "check.mjs"), str(out), PASS],
                         capture_output=True, text=True, check=True, timeout=120)  # fmt: skip
    r = json.loads(res.stdout)
    assert r["passOk"] is True and r["wrongPassOk"] is False
    s = r["searches"]
    assert s[""]["n"] == 7
    assert s["bitcoin"]["titles"] == ["This note has an attachment"]
    assert s['"in a subfolder"']["titles"] == ["This is a note in a subfolder"]
    assert s["tag:travel"]["titles"] == ["This note has tags"]
    assert set(s["folder:Folder2"]["titles"]) == {
        "This is a note in a subfolder",
        "This is a deeply buried note",
    }
    assert s["has:file"]["n"] == 1
    assert s["is:deleted"]["n"] == 0  # deleted notes are not exported by default
    assert s["-note"]["n"] == 0
    assert s["after:2025-08-02"]["titles"] == ["This note has an attachment"]
    assert r["note"]["typeofId"] == "number" and r["note"]["typeofModified"] == "number"
    assert r["note"]["hasAttLink"]
    assert {t["tag"] for t in r["tags"]} == {"travel", "vacation"}
    assert sum(f["n"] for f in r["folders"]) == 7
