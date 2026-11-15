"""Gemini proofreading client: prompt, response schema, batching, rate limiting.

The model returns JSON only (never XML). ``xmlio`` turns it into <error> tags.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

log = logging.getLogger(__name__)

# Verified with client.models.list() + live calls on a free-tier key (Nov 2026):
# gemini-2.5-flash -> 404 for new users; gemini-3.8/3.7-flash -> 503 / hang under load;
# gemini-3.5-flash and gemini-3.1-flash-lite answer in ~4 s. The chain falls through on
# 404 / 503 / timeout, so a retired or overloaded model never breaks a run.
DEFAULT_MODEL = "gemini-3.5-flash,gemini-3.8-flash,gemini-3.1-flash-lite"
REQUEST_TIMEOUT_S = 90

ErrorType = Literal["grammar", "spelling", "punctuation", "capitalization", "clarity", "styleguide"]


class Issue(BaseModel):
    original: str
    correction: str
    type: ErrorType
    # Required in the schema (the prompt demands a reason for every error). Code still
    # treats "" as "no reason" and then omits the attribute.
    reason: str


class ParagraphResult(BaseModel):
    id: int
    errors: list[Issue]


class BatchResult(BaseModel):
    results: list[ParagraphResult]


# ---------------------------------------------------------------------- prompt

_RULES = """\
You are a meticulous professional copy editor for a legal and business publisher.

TASK
You receive a JSON array of paragraphs, each {"id", "text"}. For every paragraph, list the
errors in its text. Categories (use exactly these values for "type"):
- spelling: misspelled words.
- grammar: agreement, tense, wrong word form, article misuse (a/an), commonly confused words
  (then/than, their/there, effect/affect, its/it's when grammatical).
- punctuation: missing/extra/wrong punctuation, apostrophes in possessives and contractions.
- capitalization: proper nouns, days, months, sentence starts.
- clarity: redundant, wordy or ambiguous phrasing. Correction is a concise rewrite.
- styleguide: anything that violates the HOUSE STYLE GUIDE below (dates, numbers, time,
  currency, hyphenation, preferred word usage, acronyms, quotes).
If an error is both a general error and a style-guide rule, use "styleguide".

OUTPUT RULES (strict)
1. "original" MUST be copied character-for-character from the paragraph text (same case,
   spacing, quotes and apostrophes). Never paraphrase it.
2. "original" is the SMALLEST span that contains the error, usually one word or a short phrase
   (e.g. "january, 2024" -> span "January, 2024"; "it's" -> span "it's"). Never a whole sentence
   unless the error is clarity and needs it.
3. "correction" replaces "original" exactly; it must differ from it. Corrections use curly
   apostrophes and quotes (’ “ ”), never straight ones (house style).
4. "reason": one short sentence (max 15 words) explaining the error. For "styleguide", name the
   violated rule (e.g. "No comma between month and year in dates").
5. List errors in the order they appear in the paragraph. Spans must not overlap.
6. If a paragraph is correct, return an empty "errors" list for it. Do NOT invent errors and do
   NOT flag correct text, optional stylistic preferences, or anything not covered by the rules.
7. Return one result per input id.

METHOD (do this for every paragraph before answering)
a. Read it once for spelling, grammar (subject-verb agreement, a/an, then/than, their/there,
   its/it's, affect/effect), capitalization and missing apostrophes in possessives.
b. Then scan systematically against the style guide: every date (no comma between month and
   year; "May 12, 2021" needs a comma before the year), every number (commas for 1,000+, spell
   out one to ninety-nine, "million" spelled out), every time (lowercase "a.m."/"p.m." with a
   space), every currency amount, every hyphenated or compound word and every term in the
   preferred word list, every abbreviation and acronym (exact periods, e.g. "U.C.C.").
c. Self-check each error before returning it: the correction must follow the rule it cites,
   must never contradict the style guide (if the text already uses the preferred form, e.g.
   "toward", it is NOT an error), and must keep the same numeric value ("1200" -> "1,200").
   Drop anything you are not sure is an error.

DO NOT FLAG
- Spelled-out numbers from one to ninety-nine (e.g. "fifteen percent" is correct).
- "who"/"which" vs "that", serial commas, or other optional preferences not in the style guide.
- Text that is already correct under the rules above.

PRECEDENCE AND EXCEPTIONS
- The style guide overrides general preferences.
- Never edit text inside quotation marks (quoted material stays as in the source).
- Keep source usage in names of laws, statutes, regulations, organizations and titles of works.

LOCALE
- en, en-US (and any en-* other than en-GB/en-AU/en-NZ/en-IE): American English spelling
  (e.g. "organisation" -> "organization") plus the full style guide.
- en-GB, en-AU, en-NZ, en-IE: that variety's spelling; apply the style guide except spelling preferences.
- Any other language: proofread by that language's grammar, spelling, punctuation and
  typography. Apply only the language-neutral style rules (dates, numbers, currency, time,
  quotation marks). The English preferred-word list does NOT apply. Write "reason" in English.
"""

_EXAMPLE_IN = [
    {"id": 0, "text": "My name is john and I work at the new york office."},
    {"id": 1, "text": "The firm reported approximately $2,000,000 in revenue for January, 2024, "
                      "and plans to expand it's publicly-owned subsidiaries."},
    {"id": 2, "text": "The quarterly report was submitted on time and all metrics exceeded expectations."},
]
_EXAMPLE_OUT = {"results": [
    {"id": 0, "errors": [
        {"original": "john", "correction": "John", "type": "capitalization",
         "reason": "Proper nouns are capitalized"},
        {"original": "new york", "correction": "New York", "type": "capitalization",
         "reason": "City names are proper nouns"},
    ]},
    {"id": 1, "errors": [
        {"original": "$2,000,000", "correction": "$2 million", "type": "styleguide",
         "reason": "Spell out million, billion, etc."},
        {"original": "January, 2024", "correction": "January 2024", "type": "styleguide",
         "reason": "No comma between month and year"},
        {"original": "it's", "correction": "its", "type": "punctuation",
         "reason": "Possessive its has no apostrophe"},
        {"original": "publicly-owned", "correction": "publicly owned", "type": "styleguide",
         "reason": "Do not hyphenate ly adverbs"},
    ]},
    {"id": 2, "errors": []},
]}


def build_system_prompt(style_guide: str) -> str:
    # Static, byte-stable prefix (guide first) so implicit caching can apply across requests.
    return (
        "HOUSE STYLE GUIDE\n=================\n" + style_guide + "\n\n" + _RULES
        + "\nEXAMPLE INPUT\n" + json.dumps(_EXAMPLE_IN, ensure_ascii=False)
        + "\nEXAMPLE OUTPUT\n" + json.dumps(_EXAMPLE_OUT, ensure_ascii=False)
    )


def build_user_prompt(lang: str, paragraphs: list[tuple[int, str]]) -> str:
    items = [{"id": i, "text": t} for i, t in paragraphs]
    return f"Locale: {lang}\nParagraphs:\n{json.dumps(items, ensure_ascii=False)}"


# ---------------------------------------------------------------------- client


@dataclass
class Usage:
    model: str = ""  # model that served the last successful request
    requests: int = 0
    failed_requests: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    thinking_tokens: int = 0

    def add(self, meta) -> None:
        if meta is None:
            return
        self.prompt_tokens += meta.prompt_token_count or 0
        self.output_tokens += meta.candidates_token_count or 0
        self.cached_tokens += meta.cached_content_token_count or 0
        self.thinking_tokens += meta.thoughts_token_count or 0


class RateLimiter:
    """Spaces request starts at least 60/rpm seconds apart (free tier is RPM-limited)."""

    def __init__(self, rpm: int):
        self.interval = 60.0 / rpm if rpm > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            await asyncio.sleep(delay)


@dataclass
class Proofreader:
    api_key: str
    style_guide: str
    lang: str
    model: str = DEFAULT_MODEL  # comma-separated fallback chain
    # Measured on sample_input.xml: "low" ~29 s and occasionally contradicts the guide
    # (toward -> towards); "medium" ~40 s with no such errors across runs.
    thinking: Literal["low", "medium", "high"] = "medium"
    concurrency: int = 2
    rpm: int = 10
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self):
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(
            api_key=self.api_key,
            http_options=types.HttpOptions(
                # SDK-level retry with exponential backoff + jitter on rate limit / server errors.
                retry_options=types.HttpRetryOptions(
                    attempts=4, initial_delay=2.0, max_delay=30.0,
                    http_status_codes=[408, 429, 500, 502, 503, 504],
                ),
                timeout=REQUEST_TIMEOUT_S * 1000,  # ms
            ),
        )
        self.models = [m.strip() for m in self.model.split(",") if m.strip()]
        if not self.models:
            raise ValueError("no model given")
        self._active = 0  # index into self.models; sticky once a fallback happens
        self._system = build_system_prompt(self.style_guide)
        self._sem = asyncio.Semaphore(self.concurrency)
        self._limiter = RateLimiter(self.rpm)

    @property
    def active_model(self) -> str:
        return self.models[self._active]

    def _config(self, model: str):
        t = self._types
        cfg = dict(
            system_instruction=self._system,
            response_mime_type="application/json",
            response_schema=BatchResult,
        )
        # Gemini 3 takes a thinking level; 2.5 takes a token budget (-1 = dynamic).
        if model.startswith("gemini-2.5"):
            budget = {"low": 0 if "pro" not in model else 128, "medium": 2048, "high": -1}[self.thinking]
            cfg["thinking_config"] = t.ThinkingConfig(thinking_budget=budget)
        else:
            cfg["thinking_config"] = t.ThinkingConfig(thinking_level=self.thinking)
        return t.GenerateContentConfig(**cfg)

    @staticmethod
    def _model_unavailable(e: Exception) -> bool:
        """Errors that mean "try another model" rather than "bad request"."""
        if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
            return True
        code = getattr(e, "code", None)
        return code in (404, 503)

    async def _generate(self, prompt: str):
        while True:
            idx = self._active
            model = self.models[idx]
            try:
                async with self._sem:
                    await self._limiter.wait()
                    self.usage.requests += 1
                    # The SDK timeout covers one HTTP attempt; this bounds the whole retry loop.
                    resp = await asyncio.wait_for(
                        self._client.aio.models.generate_content(
                            model=model, contents=prompt, config=self._config(model)
                        ),
                        timeout=REQUEST_TIMEOUT_S * 2,
                    )
                self.usage.model = model
                return resp
            except Exception as e:  # noqa: BLE001
                if not self._model_unavailable(e) or idx + 1 >= len(self.models):
                    raise
                if self._active == idx:  # another task may already have switched
                    self._active = idx + 1
                    log.warning("model %s unavailable (%s); falling back to %s",
                                model, getattr(e, "code", type(e).__name__), self.models[idx + 1])

    async def _call(self, paragraphs: list[tuple[int, str]]) -> dict[int, list[Issue]]:
        prompt = build_user_prompt(self.lang, paragraphs)
        log.debug("request ids=%s\n%s", [i for i, _ in paragraphs], prompt)
        resp = await self._generate(prompt)
        self.usage.add(resp.usage_metadata)
        log.debug("response: %s", resp.text)
        parsed = resp.parsed
        if not isinstance(parsed, BatchResult):
            parsed = BatchResult.model_validate_json(resp.text or "")
        return {r.id: r.errors for r in parsed.results}

    async def proofread(self, paragraphs: list[tuple[int, str]]) -> dict[int, list[Issue]]:
        """Proofread one batch. Missing/failed ids are retried one by one; ids that
        still fail are absent from the returned dict (caller leaves them unchanged)."""
        wanted = {i for i, _ in paragraphs}
        try:
            out = await self._call(paragraphs)
        except Exception as e:  # noqa: BLE001 - any API/parse failure triggers the fallback
            self.usage.failed_requests += 1
            log.warning("batch %s failed (%s: %s)", sorted(wanted), type(e).__name__, e)
            out = {}
        out = {i: v for i, v in out.items() if i in wanted}
        missing = [(i, t) for i, t in paragraphs if i not in out]
        if missing and len(paragraphs) > 1:
            log.info("retrying %d paragraph(s) individually", len(missing))
            for sub in await asyncio.gather(*(self._single(p) for p in missing)):
                out.update(sub)
        return out

    async def _single(self, para: tuple[int, str]) -> dict[int, list[Issue]]:
        try:
            return {k: v for k, v in (await self._call([para])).items() if k == para[0]}
        except Exception as e:  # noqa: BLE001
            self.usage.failed_requests += 1
            log.error("paragraph %d failed (%s: %s); left unchanged", para[0], type(e).__name__, e)
            return {}


def api_key_from_env() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
