"""Where the notes come from: a Notes group container or an iPhone backup.

Both expose the same two things: a NoteStore.sqlite copy we can open safely,
and a way to find media files by their relative path inside the container.
"""

from __future__ import annotations

import logging
import plistlib
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

NOTES_DOMAIN = "AppDomainGroup-group.com.apple.notes"
MAC_CONTAINER = Path.home() / "Library/Group Containers/group.com.apple.notes"
MAC_BACKUP_ROOT = Path.home() / "Library/Application Support/MobileSync/Backup"


class SourceError(Exception):
    """The input cannot be used; the message says why and what to do."""


@dataclass
class Source:
    """A readable NoteStore copy plus a relative-path index of container files."""

    notestore: Path
    files: dict[str, Path] = field(default_factory=dict)  # container relpath -> real path
    _tmp: tempfile.TemporaryDirectory[str] | None = None

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()

    def media_dir_files(self, media_uuid: str) -> list[Path]:
        """Files under Accounts/*/Media/<uuid>/ (or legacy Media/<uuid>/)."""
        needle = f"Media/{media_uuid}/"
        return sorted(p for rel, p in self.files.items() if needle in rel)

    def fallback_files(self, attachment_uuid: str) -> list[Path]:
        """FallbackImages / Previews for drawings and scans, largest first."""
        hits = [
            p
            for rel, p in self.files.items()
            if ("FallbackImages/" in rel or "Previews/" in rel) and attachment_uuid in rel
        ]
        return sorted(hits, key=lambda p: p.stat().st_size, reverse=True)


def _copy_db(src: Path) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    """Copy NoteStore (+ WAL/SHM) to a temp dir so the live DB is never touched."""
    tmp = tempfile.TemporaryDirectory(prefix="notedump-")
    dst = Path(tmp.name) / "NoteStore.sqlite"
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        side = src.with_name(src.name + suffix)
        if side.exists():
            shutil.copy2(side, dst.with_name(dst.name + suffix))
    return dst, tmp


def from_container(container: Path | None = None) -> Source:
    """Open a Notes group container (macOS default when container is None)."""
    root = (container or MAC_CONTAINER).expanduser()
    db = root / "NoteStore.sqlite"
    try:
        exists = db.is_file()
    except PermissionError:
        exists = False
    if not exists:
        raise SourceError(
            f"NoteStore.sqlite not found or not readable in {root}. "
            "On macOS, grant your terminal Full Disk Access "
            "(System Settings > Privacy & Security > Full Disk Access)."
        )
    copy, tmp = _copy_db(db)
    files = {str(p.relative_to(root)): p for p in _walk(root / "Accounts")}
    files.update({str(p.relative_to(root)): p for p in _walk(root / "Media")})
    return Source(notestore=copy, files=files, _tmp=tmp)


def _walk(base: Path) -> Iterator[Path]:
    if base.is_dir():
        yield from (p for p in base.rglob("*") if p.is_file() and not p.name.startswith("."))


def latest_backup(root: Path = MAC_BACKUP_ROOT) -> Path:
    """Most recently modified backup folder under the Finder/iTunes backup root."""
    try:
        dirs = [d for d in root.iterdir() if d.is_dir() and _find_ci(d, "Manifest.db")]
    except (FileNotFoundError, PermissionError) as exc:
        raise SourceError(
            f"Cannot list backups in {root} ({exc.__class__.__name__}). "
            "Pass the backup folder explicitly, and on macOS grant Full Disk Access."
        ) from exc
    if not dirs:
        raise SourceError(f"No iPhone backups found in {root}.")
    return max(dirs, key=lambda d: d.stat().st_mtime)


def _find_ci(directory: Path, name: str) -> Path | None:
    """Case-insensitive lookup. AIDEV-NOTE: backups use Manifest.db; Linux is case-sensitive."""
    for child in directory.iterdir():
        if child.name.lower() == name.lower():
            return child
    return None


def from_backup(backup_dir: Path) -> Source:
    """Open an unencrypted iPhone backup made by Finder, iTunes, or idevicebackup2."""
    backup_dir = backup_dir.expanduser()
    if not backup_dir.is_dir():
        raise SourceError(f"Backup folder not found: {backup_dir}")
    _reject_encrypted(backup_dir)
    manifest = _find_ci(backup_dir, "Manifest.db")
    if manifest is None:
        raise SourceError(f"No Manifest.db in {backup_dir}. Is this a backup folder?")

    try:
        con = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT fileID, relativePath FROM Files WHERE domain = ? AND flags = 1",
            (NOTES_DOMAIN,),
        ).fetchall()
        con.close()
    except sqlite3.DatabaseError as exc:
        raise SourceError(
            f"Cannot read {manifest} ({exc}). Encrypted backups are not supported yet."
        ) from exc

    files: dict[str, Path] = {}
    for file_id, rel in rows:
        real = backup_dir / file_id[:2] / file_id
        if real.is_file():
            files[rel] = real
    db = files.get("NoteStore.sqlite")
    if db is None:
        raise SourceError("NoteStore.sqlite is not in this backup. Is Notes on the iPhone?")
    copy, tmp = _copy_db(db)
    log.info("backup %s: %d Notes files indexed", backup_dir.name, len(files))
    return Source(notestore=copy, files=files, _tmp=tmp)


def _reject_encrypted(backup_dir: Path) -> None:
    plist = _find_ci(backup_dir, "Manifest.plist")
    if plist is None:
        return
    try:
        with plist.open("rb") as fh:
            data = plistlib.load(fh)
    except (plistlib.InvalidFileException, OSError):
        return
    if data.get("IsEncrypted"):
        raise SourceError(
            "This backup is encrypted. Make an unencrypted backup "
            "(Finder > your iPhone > uncheck 'Encrypt local backup'), "
            "or export on the Mac with `notedump local`."
        )
