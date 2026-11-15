"""XML loading/saving, <p> extraction and <error> injection.

Invariant guarantee: the LLM never returns markup. We only *wrap* character
ranges that already exist in a paragraph's text, so the text content (with
<error> tags removed) is identical by construction. ``annotate`` still
verifies this after every paragraph and restores the original on mismatch.
"""

from __future__ import annotations

import codecs
import copy
import io
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from lxml import etree

_PARSER_OPTS = dict(
    strip_cdata=False,
    remove_blank_text=False,
    remove_comments=False,
    remove_pis=False,
    resolve_entities=False,
    no_network=True,
    huge_tree=True,
)
_DECL = re.compile(rb"^(<\?xml[^>]*\?>)(\s*)")


class IssueLike(Protocol):
    original: str
    correction: str
    type: str
    reason: str | None

@dataclass
class Document:
    tree: etree._ElementTree
    encoding: str
    prolog: bytes
    trailing_newline: bool


def load(path: str | Path) -> Document:
    """Parse an XML file. Raises OSError / etree.XMLSyntaxError."""
    data = Path(path).read_bytes()
    tree = etree.parse(io.BytesIO(data), etree.XMLParser(**_PARSER_OPTS))
    bom = codecs.BOM_UTF8 if data.startswith(codecs.BOM_UTF8) else b""
    m = _DECL.match(data[len(bom):])
    prolog = bom + (m.group(1) + m.group(2) if m else b"")
    return Document(
        tree=tree,
        encoding=tree.docinfo.encoding or "UTF-8",
        prolog=prolog,
        trailing_newline=data.endswith(b"\n"),
    )


def serialize(doc: Document) -> bytes:
    body = etree.tostring(doc.tree, encoding=doc.encoding, xml_declaration=False)
    out = doc.prolog + body
    if doc.trailing_newline and not out.endswith(b"\n"):
        out += b"\n"
    return out


def save(doc: Document, path: str | Path) -> None:
    """Write atomically: temp file in the same dir, then rename (no partial output)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(serialize(doc))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

def _is_p(el) -> bool:
    return isinstance(el.tag, str) and etree.QName(el).localname == "p"


@dataclass
class Paragraph:
    element: etree._Element
    segments: list[tuple[etree._Element, str]]
    text: str
    full_text: str
    has_cdata: bool


def _segments(p) -> list[tuple[etree._Element, str]]:
    segs = [(p, "text")]

    def walk(parent):
        for child in parent:
            if isinstance(child.tag, str) and not _is_p(child):
                segs.append((child, "text"))
                walk(child)
            segs.append((child, "tail"))

    walk(p)
    return segs


def _seg_value(seg) -> str:
    node, attr = seg
    return getattr(node, attr) or ""


def find_paragraphs(doc: Document) -> list[Paragraph]:
    """All <p> elements (any namespace) in document order, including nested ones."""
    out = []
    for el in doc.tree.getroot().iter():
        if not _is_p(el):
            continue
        segs = _segments(el)
        out.append(
            Paragraph(
                element=el,
                segments=segs,
                text="".join(_seg_value(s) for s in segs),
                full_text="".join(el.itertext()),
                has_cdata=b"<![CDATA[" in etree.tostring(el, with_tail=False),
            )
        )
    return out

@dataclass
class Span:
    start: int
    end: int
    issue: IssueLike


_QUOTE_CLASS = {
    "'": "['‘’]", "‘": "['‘’]", "’": "['‘’]",
    '"': '["“”]', "“": '["“”]', "”": '["“”]',
}


def _fuzzy_pattern(s: str) -> str:
    """Whitespace runs match any whitespace; straight/curly quotes match each other."""
    parts = []
    for tok in re.split(r"(\s+)", s.strip()):
        if not tok:
            continue
        if tok.isspace():
            parts.append(r"\s+")
        else:
            parts.append("".join(_QUOTE_CLASS.get(c, re.escape(c)) for c in tok))
    return "".join(parts)


def quoted_ranges(text: str) -> list[tuple[int, int]]:
    """Content ranges inside paired double quotes. Apostrophes are never boundaries;
    an unmatched quote masks nothing."""
    ranges = []
    open_at = None
    for i, ch in enumerate(text):
        if ch == "“":
            open_at = i
        elif ch == "”" and open_at is not None:
            ranges.append((open_at + 1, i))
            open_at = None
    straight = [i for i, ch in enumerate(text) if ch == '"']
    ranges += [(a + 1, b) for a, b in zip(straight[0::2], straight[1::2])]
    return ranges


_NUMERIC = re.compile(r"[\d.,\s]+")


def _changes_number_value(original: str, correction: str) -> bool:
    """Pure number reformatting (e.g. 1200 -> 1,200) must keep the same digits.
    Guards against hallucinated values such as 1200 -> 1,000."""
    if not (_NUMERIC.fullmatch(original) and _NUMERIC.fullmatch(correction or "")):
        return False
    digits = lambda s: re.sub(r"\D", "", s)  # noqa: E731
    return digits(original) != digits(correction)


def locate_spans(text: str, issues: list[IssueLike]) -> tuple[list[Span], Counter]:
    """Map LLM issues to exact, non-overlapping character ranges of `text`.

    Issues are expected in reading order, so a moving cursor disambiguates
    repeated words. Returns (spans sorted by start, Counter of drop causes).
    """
    spans: list[Span] = []
    drops: Counter = Counter()
    quoted = quoted_ranges(text)
    cursor = 0

    def free(s, e):
        return all(e <= sp.start or s >= sp.end for sp in spans)

    for issue in issues:
        orig = issue.original or ""
        if not orig.strip():
            drops["empty"] += 1
            continue
        if issue.correction == orig:
            drops["no_change"] += 1
            continue
        if issue.type == "styleguide" and not (issue.reason or "").strip():
            drops["styleguide_without_reason"] += 1
            continue
        if _changes_number_value(orig, issue.correction):
            drops["number_value_changed"] += 1
            continue

        found = None
        seen = set()
        for pattern in (re.escape(orig), _fuzzy_pattern(orig)):
            for start in (cursor, 0):
                for m in re.finditer(pattern, text[start:]):
                    s, e = start + m.start(), start + m.end()
                    if not free(s, e):
                        seen.add("overlap")
                    elif any(qa <= s and e <= qb for qa, qb in quoted):
                        seen.add("inside_quotation")
                    else:
                        found = (s, e)
                        break
                if found:
                    break
            if found:
                break
        if not found:
            cause = "inside_quotation" if "inside_quotation" in seen else ("overlap" if seen else "not_found")
            drops[cause] += 1
            continue

        s, e = found
        spans.append(Span(s, e, issue))
        cursor = max(cursor, e)

    spans.sort(key=lambda sp: sp.start)
    return spans, drops

class InvariantError(Exception):
    """Text content changed during injection (should never happen)."""


def _make_error(parent, tag: str, issue: IssueLike, text: str):
    attrib = {"type": issue.type, "correction": issue.correction}
    if issue.reason:
        attrib["reason"] = issue.reason
    el = etree.SubElement(parent, tag, attrib)
    el.text = text
    return el


def _inject_segment(seg, error_tag: str, spans: list[tuple[int, int, IssueLike]]) -> None:
    node, attr = seg
    text = _seg_value(seg)
    if attr == "text":
        parent, pos = node, 0
    else:
        parent = node.getparent()
        pos = parent.index(node) + 1
    for i, (s, e, issue) in enumerate(spans):
        el = _make_error(parent, error_tag, issue, text[s:e])
        nxt = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        el.tail = text[e:nxt] or None
        parent.insert(pos + i, el)  # moves it from the end to its place
    setattr(node, attr, text[: spans[0][0]] or None)


@dataclass
class AnnotateResult:
    applied: list[IssueLike] = field(default_factory=list)
    drops: Counter = field(default_factory=Counter)


def annotate(para: Paragraph, issues: list[IssueLike]) -> AnnotateResult:
    """Wrap each located issue in <error>. Raises InvariantError (after restoring
    the paragraph) if the text content would change."""
    spans, drops = locate_spans(para.text, issues)
    result = AnnotateResult(drops=drops)
    if not spans:
        return result

    bounds, off = [], 0
    for seg in para.segments:
        n = len(_seg_value(seg))
        bounds.append((off, off + n))
        off += n
    per_seg: dict[int, list] = {}
    for sp in spans:
        idx = next((i for i, (a, b) in enumerate(bounds) if a <= sp.start and sp.end <= b), None)
        if idx is None:
            drops["crosses_markup"] += 1
            continue
        a = bounds[idx][0]
        per_seg.setdefault(idx, []).append((sp.start - a, sp.end - a, sp.issue))
        result.applied.append(sp.issue)
    if not per_seg:
        return result

    p = para.element
    q = etree.QName(p)
    error_tag = f"{{{q.namespace}}}error" if q.namespace else "error"
    backup = copy.deepcopy(p)
    for idx, seg_spans in per_seg.items():
        _inject_segment(para.segments[idx], error_tag, seg_spans)

    if "".join(p.itertext()) != para.full_text:
        p.text = backup.text
        p[:] = list(backup)
        raise InvariantError("text content changed; paragraph restored")
    return result
