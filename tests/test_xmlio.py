import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from lxml import etree

from xml_proofread import xmlio
from xml_proofread.styleguide import load_style_guide

EX = Path(__file__).resolve().parent.parent / "examples"


@dataclass
class I:  # minimal Issue stand-in (duck-typed by xmlio)
    original: str
    correction: str
    type: str = "spelling"
    reason: str = ""


def doc_from(xml: str, tmp_path) -> xmlio.Document:
    f = tmp_path / "in.xml"
    f.write_text(xml, encoding="utf-8")
    return xmlio.load(f)


def annotate_first(xml, issues, tmp_path):
    doc = doc_from(xml, tmp_path)
    para = xmlio.find_paragraphs(doc)[0]
    res = xmlio.annotate(para, issues)
    return xmlio.serialize(doc).decode(), res


def strip_errors(s: str) -> str:
    return re.sub(r"</?error[^>]*>", "", s)


@pytest.mark.parametrize("name", ["example_input", "example_output", "sample_input"])
def test_roundtrip_is_byte_identical(name):
    src = (EX / f"{name}.xml").read_bytes()
    assert xmlio.serialize(xmlio.load(EX / f"{name}.xml")) == src


def test_golden_injection_reproduces_example_output():
    """Feed the errors from example_output.xml back in: output must match byte-for-byte."""
    expected = xmlio.load(EX / "example_output.xml")
    doc = xmlio.load(EX / "example_input.xml")
    for para, exp in zip(xmlio.find_paragraphs(doc), xmlio.find_paragraphs(expected)):
        issues = [
            I(e.text, e.get("correction"), e.get("type"), e.get("reason") or "")
            for e in exp.element if e.tag == "error"
        ]
        xmlio.annotate(para, issues)
    assert xmlio.serialize(doc) == (EX / "example_output.xml").read_bytes()


def test_basic_capitalization(tmp_path):
    out, res = annotate_first("<r><p>My name is john.</p></r>", [I("john", "John", "capitalization")], tmp_path)
    assert '<p>My name is <error type="capitalization" correction="John">john</error>.</p>' in out
    assert len(res.applied) == 1


def test_reason_attribute_only_when_present(tmp_path):
    out, _ = annotate_first(
        "<r><p>a teh b teh</p></r>",
        [I("teh", "the", reason="Misspelling"), I("teh", "the")],
        tmp_path,
    )
    assert '<error type="spelling" correction="the" reason="Misspelling">teh</error> b ' in out
    assert out.endswith('<error type="spelling" correction="the">teh</error></p></r>\n') or \
        out.endswith('<error type="spelling" correction="the">teh</error></p></r>')


def test_duplicate_words_resolved_in_order(tmp_path):
    out, res = annotate_first(
        "<r><p>it's fine and it's ok</p></r>",
        [I("it's", "its", "punctuation"), I("it's", "its", "punctuation")],
        tmp_path,
    )
    assert out.count("<error") == 2 and len(res.applied) == 2


def test_overlap_and_not_found_dropped(tmp_path):
    out, res = annotate_first(
        "<r><p>new york city</p></r>",
        [I("new york", "New York", "capitalization"), I("york", "York", "capitalization"), I("boston", "Boston")],
        tmp_path,
    )
    assert out.count("<error") == 1
    assert res.drops["overlap"] == 1 and res.drops["not_found"] == 1


def test_no_change_and_empty_and_styleguide_without_reason_dropped(tmp_path):
    out, res = annotate_first(
        "<r><p>alpha beta gamma</p></r>",
        [I("alpha", "alpha"), I("  ", "x"), I("beta", "Beta", "styleguide", "")],
        tmp_path,
    )
    assert "<error" not in out
    assert res.drops == {"no_change": 1, "empty": 1, "styleguide_without_reason": 1}


def test_fuzzy_whitespace_and_quote_match(tmp_path):
    xml = "<r><p>the  committe’s\n   report</p></r>"
    out, res = annotate_first(xml, [I("the committe's report", "the committee’s report")], tmp_path)
    assert "<error" in out and "committe’s\n   report</error>" in out


def test_span_crossing_inline_markup_dropped(tmp_path):
    out, res = annotate_first("<r><p>a <b>bold wrd</b>s end</p></r>", [I("wrds", "words")], tmp_path)
    assert "<error" not in out and res.drops["crosses_markup"] == 1 and not res.applied


def test_span_inside_inline_element_kept(tmp_path):
    out, _ = annotate_first("<r><p>a <b>bold wrd</b>s end</p></r>", [I("wrd", "word")], tmp_path)
    assert '<b>bold <error type="spelling" correction="word">wrd</error></b>s end' in out


def test_error_in_tail_of_inline_element(tmp_path):
    xml = "<r><p>a <b>b</b> teh end</p></r>"
    out, _ = annotate_first(xml, [I("teh", "the")], tmp_path)
    assert "<b>b</b> <error" in out and strip_errors(out).count("teh end") == 1


def test_nested_p_is_its_own_unit(tmp_path):
    doc = doc_from("<r><p>outr <p>innr</p> tail</p></r>", tmp_path)
    outer, inner = xmlio.find_paragraphs(doc)
    assert outer.text == "outr  tail" and inner.text == "innr"
    xmlio.annotate(inner, [I("innr", "inner")])
    xmlio.annotate(outer, [I("outr", "outer")])
    out = xmlio.serialize(doc).decode()
    assert out == ('<r><p><error type="spelling" correction="outer">outr</error> '
                   '<p><error type="spelling" correction="inner">innr</error></p> tail</p></r>')


def test_default_namespace_gives_clean_error_tag(tmp_path):
    out, _ = annotate_first('<r xmlns="urn:x"><p>teh</p></r>', [I("teh", "the")], tmp_path)
    assert '<error type="spelling" correction="the">teh</error>' in out
    assert 'xmlns=""' not in out and "ns0" not in out


def test_prefixed_namespaces_and_cdata_preserved(tmp_path):
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<r xmlns:m="urn:m"><m:info a="1">x</m:info>'
           '<code><![CDATA[a < b]]></code><!-- c --><p>teh</p></r>\n')
    out, _ = annotate_first(xml, [I("teh", "the")], tmp_path)
    assert out == xml.replace("<p>teh</p>", '<p><error type="spelling" correction="the">teh</error></p>')


def test_cdata_paragraph_flagged(tmp_path):
    doc = doc_from("<r><p><![CDATA[raw teh]]></p></r>", tmp_path)
    assert xmlio.find_paragraphs(doc)[0].has_cdata


def test_attribute_values_escaped(tmp_path):
    out, _ = annotate_first('<r><p>he said hi</p></r>', [I("hi", 'say "hi"', "clarity", 'Use "quotes" & <care>')], tmp_path)
    assert 'correction="say &quot;hi&quot;"' in out and "&amp; &lt;care&gt;" in out
    assert etree.fromstring(out.encode()) is not None


def test_quoted_material_filter(tmp_path):
    xml = "<r><p>He said “teh rule” and \"teh law\" but teh fact; it's 1930's advisor's Workers' \"dangling teh</p></r>"
    issues = [
        I("teh", "the"),  # inside curly quotes -> dropped
        I("teh", "the"),  # inside straight quotes -> dropped
        I("teh", "the"),  # plain -> kept
        I("it's", "its"), I("1930's", "1930s", "styleguide", "No apostrophe for plural years"),
        I("advisor's", "advisors"), I("Workers'", "Workers’"),
        I("teh", "the"),  # after dangling quote -> kept
    ]
    out, res = annotate_first(xml, issues, tmp_path)
    # Both unquoted "teh" get wrapped, quoted ones never; apostrophe words are kept.
    assert "“teh rule”" in out and '"teh law"' in out
    assert out.count('correction="the">teh</error>') == 2
    assert len(res.applied) == 6 and sum(res.drops.values()) == 2
    for w in ("it's", "1930's", "advisor's", "Workers'"):
        assert f">{w}</error>" in out


def test_only_quoted_occurrence_is_dropped(tmp_path):
    out, res = annotate_first("<r><p>He said “teh rule”.</p></r>", [I("teh", "the")], tmp_path)
    assert "<error" not in out and res.drops["inside_quotation"] == 1


def test_quoted_ranges_ignores_apostrophes_and_dangling():
    assert xmlio.quoted_ranges("it's ’90s 'x' “a”") == [(15, 16)]
    assert xmlio.quoted_ranges('one " dangling') == []


def test_invariant_holds_after_injection(tmp_path):
    xml = (EX / "sample_input.xml").read_text(encoding="utf-8")
    doc = doc_from(xml, tmp_path)
    for para in xmlio.find_paragraphs(doc):
        words = para.text.split()
        xmlio.annotate(para, [I(w, w.upper()) for w in words[::3]])
    out = xmlio.serialize(doc).decode()
    assert out.count("<error") > 50
    assert strip_errors(out) == xml


def test_atomic_save(tmp_path):
    doc = doc_from("<r><p>x</p></r>", tmp_path)
    target = tmp_path / "out.xml"
    xmlio.save(doc, target)
    assert target.read_bytes() == b"<r><p>x</p></r>"
    assert not list(tmp_path.glob("*.tmp"))


def test_style_guide_keeps_hierarchy():
    text = load_style_guide(EX / "style_guide.docx")
    assert "# List of Preferred Word Usage" in text
    assert text.index("acknowledgment (not acknowledgement)") > text.index("# List of Preferred Word Usage")
    assert "## Hot Spots for Errors" in text and "## Dates (Months, Days, Years)" in text


def test_number_value_guard(tmp_path):
    out, res = annotate_first(
        "<r><p>About 1200 firms and 1300 banks paid $2,000,000.</p></r>",
        [I("1200", "1,000", "styleguide", "Commas for 1,000+"),
         I("1300", "1,300", "styleguide", "Commas for 1,000+"),
         I("$2,000,000", "$2 million", "styleguide", "Spell out million")],
        tmp_path,
    )
    assert res.drops["number_value_changed"] == 1
    assert ">1300</error>" in out and ">$2,000,000</error>" in out and ">1200</error>" not in out
