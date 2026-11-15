import asyncio
import json
import shutil
from pathlib import Path

import pytest

from xml_proofread import cli, llm, pipeline, xmlio

EX = Path(__file__).resolve().parent.parent / "examples"


class StubProofreader:
    """Returns fixed issues per paragraph id; ids in `fail` are omitted (simulated API failure)."""

    def __init__(self, issues: dict[int, list[llm.Issue]], fail=()):
        self.issues, self.fail = issues, set(fail)
        self.usage = llm.Usage()
        self.batches = []

    async def proofread(self, paragraphs):
        self.batches.append([i for i, _ in paragraphs])
        return {i: self.issues.get(i, []) for i, _ in paragraphs if i not in self.fail}


def issues_from_expected() -> dict[int, list[llm.Issue]]:
    doc = xmlio.load(EX / "example_output.xml")
    return {
        i: [
            llm.Issue(original=e.text, correction=e.get("correction"), type=e.get("type"),
                      reason=e.get("reason") or "")
            for e in p.element if e.tag == "error"
        ]
        for i, p in enumerate(xmlio.find_paragraphs(doc))
    }


@pytest.fixture
def example_input(tmp_path):
    return Path(shutil.copy(EX / "example_input.xml", tmp_path / "example_input.xml"))


def test_golden_pipeline_matches_example_output(example_input):
    stub = StubProofreader(issues_from_expected())
    stats = asyncio.run(pipeline.proofread_file(example_input, stub, batch_size=2))
    out = example_input.with_name("example_input.corrected.xml")
    assert stats.output == str(out)
    assert out.read_bytes() == (EX / "example_output.xml").read_bytes()
    assert stub.batches == [[0, 1], [2, 3]]
    assert (stats.paragraphs, stats.changed, stats.clean, stats.failed) == (4, 3, 1, 0)
    assert stats.errors_by_type == {"spelling": 3, "capitalization": 3, "styleguide": 3, "punctuation": 1}
    assert stats.seconds > 0 and stats.peak_rss_mb > 1
    d = json.loads(json.dumps(stats.as_dict()))
    assert d["errors_by_type"]["spelling"] == 3 and d["errors"] == 10


def test_failed_paragraph_left_unchanged(example_input):
    stub = StubProofreader(issues_from_expected(), fail={1})
    stats = asyncio.run(pipeline.proofread_file(example_input, stub))
    out = example_input.with_name("example_input.corrected.xml").read_text()
    assert stats.failed == 1
    assert "<p>My name is john and I work at the new york office" in out


def test_output_path():
    assert pipeline.output_path("/a/b/input.xml") == Path("/a/b/input.corrected.xml")


def test_cli_rejects_bad_lang(capsys):
    with pytest.raises(SystemExit) as e:
        cli.parse_args(["x.xml", "--lang", "english!", "--style-guide", "g.docx"])
    assert e.value.code == 2


def test_cli_missing_key(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    assert cli.main(["x.xml", "--lang", "en", "--style-guide", str(EX / "style_guide.docx")]) == 2


def test_cli_bad_style_guide(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    bad = tmp_path / "g.docx"
    bad.write_text("not a zip")
    assert cli.main(["x.xml", "--lang", "en", "--style-guide", str(bad)]) == 2


def test_cli_missing_and_malformed_input(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")  # never used: files fail before any API call
    broken = tmp_path / "broken.xml"
    broken.write_text("<r><p>unclosed</r>")
    args = [str(tmp_path / "missing.xml"), str(broken), "--lang", "en", "--style-guide", str(EX / "style_guide.docx")]
    assert cli.main(args) == 2


def test_system_prompt_contains_guide_and_rules():
    p = llm.build_system_prompt("GUIDE TEXT")
    assert p.startswith("HOUSE STYLE GUIDE") and "GUIDE TEXT" in p and "OUTPUT RULES" in p


def test_model_fallback_is_sticky(monkeypatch):
    class Unavailable(Exception):
        code = 503

    calls = []

    class Resp:
        usage_metadata = None
        text = '{"results": [{"id": 0, "errors": []}]}'
        parsed = llm.BatchResult(results=[llm.ParagraphResult(id=0, errors=[])])

    async def fake_generate(model, contents, config):
        calls.append(model)
        if model == "dead-model":
            raise Unavailable("overloaded")
        return Resp()

    p = llm.Proofreader(api_key="dummy", style_guide="g", lang="en", model="dead-model, good-model", rpm=0)
    monkeypatch.setattr(p._client.aio.models, "generate_content", fake_generate)

    async def run():
        return [await p.proofread([(0, "Hi.")]), await p.proofread([(0, "Hi.")])]

    assert asyncio.run(run()) == [{0: []}, {0: []}]
    assert calls == ["dead-model", "good-model", "good-model"]
    assert p.usage.model == "good-model" and p.usage.failed_requests == 0


def test_bad_request_does_not_fall_back(monkeypatch):
    class BadRequest(Exception):
        code = 400

    calls = []

    async def fake_generate(model, contents, config):
        calls.append(model)
        raise BadRequest("invalid")

    p = llm.Proofreader(api_key="dummy", style_guide="g", lang="en", model="a,b", rpm=0)
    monkeypatch.setattr(p._client.aio.models, "generate_content", fake_generate)
    assert asyncio.run(p.proofread([(0, "Hi.")])) == {}
    assert calls == ["a"] and p.usage.failed_requests == 1
