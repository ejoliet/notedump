from apple_notes_parser import notestore_pb2 as pb

from notedump.markdown import CHECKLIST, HEADING, MONO, NUMBERED, TITLE, render


def note(*runs):
    """runs: (text, dict of attribute-run fields)."""
    n = pb.Note()
    n.note_text = "".join(t for t, _ in runs)
    for text, attrs in runs:
        r = n.attribute_run.add()
        r.length = len(text.encode("utf-16-le")) // 2
        style = attrs.get("style")
        if style is not None:
            r.paragraph_style.style_type = style
            r.paragraph_style.indent_amount = attrs.get("indent", 0)
            if "done" in attrs:
                r.paragraph_style.checklist.uuid = b"x"
                r.paragraph_style.checklist.done = attrs["done"]
        if "weight" in attrs:
            r.font_weight = attrs["weight"]
        if "link" in attrs:
            r.link = attrs["link"]
        if "att" in attrs:
            r.attachment_info.attachment_identifier = attrs["att"][0]
            r.attachment_info.type_uti = attrs["att"][1]
    return n


def no_att(ident, uti):
    return f"[{ident}](att:{ident})", ident


def test_structure():
    md, text = render(
        note(
            ("Groceries\n", {"style": TITLE}),
            ("Section\n", {"style": HEADING}),
            ("milk\n", {"style": CHECKLIST, "done": 1}),
            ("eggs\n", {"style": CHECKLIST, "done": 0}),
            ("one\n", {"style": NUMBERED}),
            ("two\n", {"style": NUMBERED}),
            ("nested\n", {"style": NUMBERED, "indent": 1}),
            ("x = 1\n", {"style": MONO}),
            ("y = 2\n", {"style": MONO}),
            ("line a\nline b\n", {}),
        ),
        no_att,
    )
    assert md.startswith(
        "# Groceries\n\n## Section\n\n- [x] milk\n- [ ] eggs\n\n1. one\n2. two\n    1. nested"
    )
    assert "```\nx = 1\ny = 2\n```" in md
    assert "line a  \nline b" in md
    assert "milk" in text and "#" not in text


def test_split_runs_merge_and_bold():
    md, _ = render(
        note(("Thi", {"weight": 1}), ("s is bold", {"weight": 1}), (" plain\n", {})), no_att
    )
    assert "**This is bold** plain" in md


def test_utf16_lengths_with_emoji():
    md, _ = render(note(("hi 🎉 ", {}), ("bold", {"weight": 1}), ("\n", {})), no_att)
    assert "hi 🎉 **bold**" in md


def test_escape_and_links():
    md, _ = render(
        note(("# not a heading *x*\n", {}), ("site", {"link": "https://a.b/c d"}), ("\n", {})),
        no_att,
    )
    assert md.startswith("\\# not a heading \\*x\\*")
    assert "[site](<https://a.b/c d>)" in md


def test_attachment_placeholder():
    md, text = render(
        note(("see ", {}), ("\ufffc", {"att": ("A1", "public.jpeg")}), ("\n", {})), no_att
    )
    assert "see [A1](att:A1)" in md
    assert "\ufffc" not in text
