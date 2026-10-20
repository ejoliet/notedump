"""notedump CLI: export Apple Notes into an encrypted folder for the web viewer."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .export import ExportError, Options, Report, run
from .sources import Source, SourceError, from_backup, from_container, latest_backup

PASS_ENV = "NOTEDUMP_PASSPHRASE"
MIN_PASS_LEN = 12


def _passphrase(out: Path, plain: bool, rekey: bool) -> str | None:
    if plain:
        return None
    if env := os.environ.get(PASS_ENV):
        return env
    if not sys.stdin.isatty():
        raise ExportError(f"No terminal for a passphrase prompt. Set {PASS_ENV} or use --plain.")
    first = getpass.getpass("Passphrase: ")
    if (out / "notedump.json").exists() and not rekey:
        return first  # existing export: checked against its manifest, no confirm needed
    if len(first) < MIN_PASS_LEN:
        raise ExportError(
            f"Use at least {MIN_PASS_LEN} characters. A short passphrase is guessable."
        )
    if getpass.getpass("Repeat passphrase: ") != first:
        raise ExportError("Passphrases do not match.")
    return first


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="notedump",
        description="Export Apple Notes to encrypted Parquet for a static, searchable viewer.",
    )
    p.add_argument("--version", action="version", version=f"notedump {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-o", "--out", type=Path, required=True,
                        help="export folder (can live in a Box/Dropbox synced folder)")  # fmt: skip
        sp.add_argument("--plain", action="store_true", help="do not encrypt (local debugging)")
        sp.add_argument("--rekey", action="store_true",
                        help="new salt and passphrase; rewrites every file")  # fmt: skip
        sp.add_argument("--include-deleted", action="store_true",
                        help="include notes in Recently Deleted")  # fmt: skip
        sp.add_argument("--no-attachments", action="store_true", help="metadata only")
        sp.add_argument("--max-attachment-mb", type=int, default=100,
                        help="skip larger files (default 100)")  # fmt: skip
        sp.add_argument("--dump", type=Path, metavar="DIR",
                        help="also write PLAINTEXT notes.parquet, attachments.parquet, notes.sqlite here")  # fmt: skip
        sp.add_argument("-v", "--verbose", action="store_true")

    lp = sub.add_parser("local", help="read this Mac's Notes (needs Full Disk Access)")
    lp.add_argument("--container", type=Path, help="Notes group container (default: macOS path)")
    common(lp)

    bp = sub.add_parser("backup", help="read an unencrypted iPhone backup")
    bp.add_argument("backup_dir", type=Path, nargs="?",
                    help="backup folder (default: newest in ~/Library/Application Support/MobileSync/Backup)")  # fmt: skip
    common(bp)
    return p


def _print_report(r: Report, out: Path, encrypted: bool) -> None:
    print(f"Exported {r.notes} notes to {out} ({'encrypted' if encrypted else 'PLAIN'})")
    skipped = [f"{r.locked_skipped} locked"]
    if r.deleted_skipped:
        skipped.append(f"{r.deleted_skipped} recently deleted")
    print(f"Skipped: {', '.join(skipped)}")
    if r.unreadable:
        print(f"Plain-text fallback for {r.unreadable} notes (format not decoded)")
    status = ", ".join(f"{v} {k}" for k, v in sorted(r.by_status.items())) or "none"
    print(f"Attachments: {r.attachments} ({status})")
    print(f"Files: {r.files_written} written, {r.files_reused} unchanged, {r.files_pruned} removed")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")  # fmt: skip
    source: Source | None = None
    try:
        pw = _passphrase(args.out, args.plain, args.rekey)
        if args.cmd == "local":
            source = from_container(args.container)
        else:
            source = from_backup(args.backup_dir or latest_backup())
        opts = Options(
            out=args.out,
            passphrase=pw,
            include_deleted=args.include_deleted,
            attachments=not args.no_attachments,
            max_attachment_bytes=args.max_attachment_mb * 1024 * 1024,
            dump_dir=args.dump,
            rekey=args.rekey,
        )
        report = run(source, opts)
    except (SourceError, ExportError) as exc:
        print(f"notedump: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        if source is not None:
            source.close()
    _print_report(report, args.out, pw is not None)
    if args.dump:
        print(
            f"WARNING: {args.dump} is plaintext (Parquet + SQLite). Keep it out of synced folders."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
