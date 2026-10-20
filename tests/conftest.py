import shutil
from pathlib import Path

import pytest

from notedump import crypto
from notedump.export import Options, run
from notedump.sources import from_container

FIXTURE = Path(__file__).parent / "fixtures" / "group.com.apple.notes"
PASS = "correct horse battery staple"


@pytest.fixture(scope="session")
def fast_kdf():
    # AIDEV-NOTE: tests shrink PBKDF2 iterations; production default stays 600k.
    old = crypto.DEFAULT_ITERATIONS
    crypto.DEFAULT_ITERATIONS = 1000
    yield
    crypto.DEFAULT_ITERATIONS = old


@pytest.fixture
def export_dir(tmp_path, fast_kdf):
    out = tmp_path / "export"
    src = from_container(FIXTURE)
    try:
        report = run(src, Options(out=out, passphrase=PASS))
    finally:
        src.close()
    return out, report


@pytest.fixture
def container_copy(tmp_path):
    dst = tmp_path / "container"
    shutil.copytree(FIXTURE, dst)
    return dst
