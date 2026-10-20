"""Turn the Apple Notes protobuf (text + attribute runs) into Markdown and plain text.

AIDEV-NOTE: run lengths are UTF-16 code units (NSString), not Python chars.
Emoji and other astral characters break naive slicing; see _split_utf16.
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from apple_notes_parser import notestore_pb2 as pb
from google.protobuf.message import DecodeError

OBJ = "\ufffc"  # object replacement char marks an attachment in note_text

# paragraph_style.style_type values used by Notes
TITLE, HEADING, SUBHEADING, MONO = 0, 1, 2, 4
DOTTED, DASHED, NUMBERED, CHECKLIST = 100, 101, 102, 103
LIST_STYLES = {DOTTED, DASHED, NUMBERED, CHECKLIST}


@dataclass
class Inline:
    text: str
    bold: bool = False
    italic: bool = False
    strike: bool = False
    link: str | None = None
    attachment: tuple[str, str] | None = None  # (identifier, type_uti)


@dataclass
class Para:
    inlines: list[Inline] = field(default_factory=list)
    style: int = -1
    indent: int = 0
    checked: bool = False
    quote: bool = False


# Callback: (identifier, type_uti) -> (markdown, plain_text)
AttachmentRenderer = Callable[[str, str], tuple[str, str]]


def parse_zdata(zdata: bytes | None) -> pb.Note | None:
    """Decode ZICNOTEDATA.ZDATA. Returns None for locked or unreadable notes."""
    if not zdata:
        return None
    try:
        raw = gzip.decompress(zdata)
    except (OSError, EOFError):
        return None  # locked notes are encrypted, not gzipped
    store = pb.NoteStoreProto()
    try:
        store.ParseFromString(raw)
    except DecodeError:  # schema drift or a non-Notes blob
        return None
    return store.document.note if store.HasField("document") else None


def _split_utf16(text: str, lengths: list[int]) -> list[str]:
    data = text.encode("utf-16-le")
    out, pos = [], 0
    for n in lengths:
        out.append(data[pos : pos + 2 * n].decode("utf-16-le", errors="replace"))
        pos += 2 * n
    if pos < len(data):  # trailing text with no run
        out.append(data[pos:].decode("utf-16-le", errors="replace"))
    return out


def _paragraphs(note: pb.Note) -> list[Para]:
    runs = list(note.attribute_run)
    pieces = _split_utf16(note.note_text, [r.length for r in runs])
    paras: list[Para] = [Para()]
    for i, piece in enumerate(pieces):
        run = runs[i] if i < len(runs) else None
        ps = run.paragraph_style if run is not None and run.HasField("paragraph_style") else None
        weight = run.font_weight if run is not None else 0
        base = Inline(
            text="",
            bold=weight in (1, 3),
            italic=weight in (2, 3),
            strike=bool(run is not None and run.strikethrough),
            link=run.link if run is not None and run.HasField("link") else None,
            attachment=(
                (run.attachment_info.attachment_identifier, run.attachment_info.type_uti)
                if run is not None and run.HasField("attachment_info")
                else None
            ),
        )
        for j, chunk in enumerate(piece.split("\n")):
            if j > 0:
                paras.append(Para())
            if chunk:
                inl = paras[-1].inlines
                prev = inl[-1] if inl else None
                if (
                    prev is not None
                    and prev.attachment is None
                    and base.attachment is None
                    and (prev.bold, prev.italic, prev.strike, prev.link)
                    == (base.bold, base.italic, base.strike, base.link)
                ):
                    prev.text += chunk  # runs split mid-word; merge equal formatting
                else:
                    inl.append(Inline(**{**base.__dict__, "text": chunk}))
            # The style of a paragraph is carried by its runs; last one wins.
            if ps is not None and (chunk or j < piece.count("\n")):
                cur = paras[-1]
                cur.style = ps.style_type if ps.HasField("style_type") else -1
                cur.indent = ps.indent_amount
                cur.checked = ps.HasField("checklist") and bool(ps.checklist.done)
                cur.quote = bool(ps.block_quote)
    if paras and not paras[-1].inlines and paras[-1].style == -1:
        paras.pop()  # trailing newline
    return paras


_ESC = re.compile(r"([\\`*_\[\]<>])")
_LEAD = re.compile(r"^(\s*)([#>+\-]|\d+[.)])(\s)")


def _escape(text: str) -> str:
    return _ESC.sub(r"\\\1", text)


def _inline_md(inl: Inline, render_att: AttachmentRenderer) -> tuple[str, str]:
    if inl.attachment is not None:
        md, plain = "", ""
        for ch in inl.text:
            if ch == OBJ:
                m, p = render_att(*inl.attachment)
                md, plain = md + m, plain + p
            else:
                md, plain = md + _escape(ch), plain + ch
        return md, plain
    md = _escape(inl.text)
    if not md.strip():
        return md, inl.text
    core, lead, trail = md.strip(), md[: len(md) - len(md.lstrip())], md[len(md.rstrip()) :]
    if inl.strike:
        core = f"~~{core}~~"
    if inl.bold and inl.italic:
        core = f"***{core}***"
    elif inl.bold:
        core = f"**{core}**"
    elif inl.italic:
        core = f"*{core}*"
    if inl.link:
        core = f"[{core}](<{inl.link}>)"
    return lead + core + trail, inl.text


def render(note: pb.Note, render_att: AttachmentRenderer) -> tuple[str, str]:
    """Return (markdown, plain_text) for a parsed note."""
    out: list[str] = []
    plain: list[str] = []
    prev_kind: str | None = None
    prev_list_style = -2
    numbers: dict[int, int] = {}

    for para in _paragraphs(note):
        parts = [_inline_md(i, render_att) for i in para.inlines]
        md_text = "".join(p[0] for p in parts)
        plain_text = "".join(p[1] for p in parts)
        plain.append(plain_text)

        if para.style == MONO:
            kind = "code"
            line = plain_text  # raw inside fences
        elif para.style in LIST_STYLES:
            kind = "list"
            pad = "    " * max(para.indent, 0)
            if para.style == NUMBERED:
                numbers = {k: v for k, v in numbers.items() if k <= para.indent}
                numbers[para.indent] = numbers.get(para.indent, 0) + 1
                marker = f"{numbers[para.indent]}."
            elif para.style == CHECKLIST:
                marker = "- [x]" if para.checked else "- [ ]"
            else:
                marker = "-"
            line = f"{pad}{marker} {md_text}".rstrip()
        elif not plain_text.strip():
            kind = "blank"
            line = ""
        elif para.style in (TITLE, HEADING, SUBHEADING):
            kind = "heading"
            line = "#" * {TITLE: 1, HEADING: 2, SUBHEADING: 3}[para.style] + " " + md_text.strip()
        else:
            kind = "text"
            line = _LEAD.sub(r"\1\\\2\3", md_text)

        if para.quote and kind not in ("code", "blank"):
            line = "> " + line
        if kind != "list":
            numbers = {}

        # Block separation: keep lists and text lines tight, separate block kinds.
        if prev_kind == "code" and kind != "code":
            out.append("```")
        if kind == "code" and prev_kind != "code":
            if out and out[-1] != "":
                out.append("")
            out.append("```")
        elif kind == "blank":
            if out and out[-1] != "":
                out.append("")
            prev_kind = kind
            continue
        elif kind == "heading" or (prev_kind not in (None, kind, "blank")):
            if out and out[-1] != "":
                out.append("")
        elif (
            kind == "list"
            and prev_kind == "list"
            and para.indent <= 0
            and para.style != prev_list_style
        ):
            out.append("")  # a different top-level list type starts a new list
        elif kind == "text" and prev_kind == "text" and out:
            out[-1] += "  "  # hard line break: Notes lines are lines
        out.append(line)
        if kind == "heading":
            out.append("")
        if kind == "list" and para.indent <= 0:
            prev_list_style = para.style
        prev_kind = kind

    if prev_kind == "code":
        out.append("```")
    md = "\n".join(out).strip() + "\n"
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md, "\n".join(plain).strip()
