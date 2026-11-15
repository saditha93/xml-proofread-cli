"""Extract a .docx style guide as lightly structured Markdown text.

Only the standard library is used: a .docx is a zip whose body lives in
``word/document.xml``. Paragraph styles and list numbering are mapped to
Markdown headings / bullets so the LLM sees which rules belong to which
section, which improves rule adherence compared to a flat text dump.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING = re.compile(r"(?:Heading|Level)\s*(\d)", re.IGNORECASE)


class StyleGuideError(Exception):
    """Raised when the style guide cannot be read."""


def _paragraph_text(p: ET.Element) -> str:
    parts = []
    for node in p.iter():
        if node.tag == W + "t" and node.text:
            parts.append(node.text)
        elif node.tag == W + "tab":
            parts.append(" ")
        elif node.tag in (W + "br", W + "cr"):
            parts.append("\n")
    return "".join(parts).strip()


def _format(p: ET.Element, text: str) -> str:
    ppr = p.find(W + "pPr")
    style = ""
    level = 0
    is_list = False
    if ppr is not None:
        ps = ppr.find(W + "pStyle")
        if ps is not None:
            style = ps.get(W + "val", "")
        num = ppr.find(W + "numPr")
        if num is not None:
            is_list = True
            ilvl = num.find(W + "ilvl")
            if ilvl is not None:
                level = int(ilvl.get(W + "val", "0"))

    centered = ppr is not None and ppr.find(f"{W}jc[@{W}val='center']") is not None
    runs = [r for r in p.iter(W + "r") if r.find(W + "t") is not None]
    all_bold = bool(runs) and all(r.find(f"{W}rPr/{W}b") is not None for r in runs)
    # Top-level sections in the guide are hand-formatted (centered + bold), not styled.
    if style == "Title" or (centered and all_bold and not text.endswith(":")):
        return f"# {text}"
    m = _HEADING.search(style)
    if m:
        return "#" * (int(m.group(1)) + 1) + " " + text
    if is_list or style == "ListParagraph":
        return "  " * level + "- " + text
    if style.endswith("Sample"):
        return "  e.g. " + text
    if style.endswith("NoteText"):
        return "  " + text
    return text


def load_style_guide(path: str | Path) -> str:
    """Return the style guide as Markdown-ish text. Raises StyleGuideError."""
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml")
    except (OSError, zipfile.BadZipFile, KeyError) as e:
        raise StyleGuideError(f"cannot read style guide {path}: {e}") from e

    body = ET.fromstring(xml).find(W + "body")
    if body is None:
        raise StyleGuideError(f"style guide {path} has no document body")

    # Table cells are emitted as plain lines (no table layout). Good enough for rule text.
    lines = []
    for p in body.iter(W + "p"):
        text = _paragraph_text(p)
        if text:
            lines.append(_format(p, text))
    if not lines:
        raise StyleGuideError(f"style guide {path} is empty")
    return "\n".join(lines)
