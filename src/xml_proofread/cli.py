"""Command-line entry point: ``xml-proofread FILE [FILE ...] --lang en --style-guide guide.docx``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path

from lxml import etree

from . import llm, pipeline
from .styleguide import StyleGuideError, load_style_guide

log = logging.getLogger("xml_proofread")

EXIT_OK, EXIT_PARTIAL, EXIT_FATAL = 0, 1, 2
_BCP47 = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def _lang(value: str) -> str:
    if not _BCP47.match(value):
        raise argparse.ArgumentTypeError(f"not a BCP-47 language tag: {value!r} (e.g. en, en-US, fr)")
    return value


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="xml-proofread",
        description="Proofread <p> elements in XML files with Gemini; writes <name>.corrected.xml.",
    )
    ap.add_argument("files", nargs="+", type=Path, help="input XML file(s)")
    ap.add_argument("--lang", required=True, type=_lang, help="BCP-47 language tag, e.g. en, en-GB, fr")
    ap.add_argument("--style-guide", required=True, type=Path, help="style guide .docx")
    ap.add_argument("--model", default=os.environ.get("GEMINI_MODEL") or llm.DEFAULT_MODEL,
                    help="Gemini model id, or a comma-separated fallback chain tried in order on "
                         "404/503/timeout (default: $GEMINI_MODEL or %(default)s)")
    ap.add_argument("--thinking", choices=["low", "medium", "high"], default="medium",
                    help="model reasoning effort: low = fastest, medium = most reliable on style rules (default)")
    ap.add_argument("--batch-size", type=_positive, default=15, help="paragraphs per request (default 15)")
    ap.add_argument("--concurrency", type=_positive, default=2, help="parallel requests (default 2)")
    ap.add_argument("--rpm", type=int, default=10, help="max requests per minute, 0 = unlimited (default 10)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging (prompts, responses, drops)")
    return ap.parse_args(argv)


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env support (KEY=VALUE lines); real env vars win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip().strip("'\"")
            if v:
                os.environ.setdefault(k.strip(), v)


def _print_summary(stats: pipeline.FileStats) -> None:
    rss = f"{stats.peak_rss_mb:.1f} MB" if stats.peak_rss_mb is not None else "n/a"
    types = ", ".join(f"{k}={v}" for k, v in sorted(stats.errors_by_type.items())) or "-"
    drops = ", ".join(f"{k}={v}" for k, v in sorted(stats.dropped.items())) or "-"
    u = stats.usage
    print(
        f"\n{stats.input} -> {stats.output}\n"
        f"  time            {stats.seconds:.2f} s   peak RSS {rss}\n"
        f"  paragraphs      {stats.paragraphs} total | {stats.changed} corrected | {stats.clean} clean | "
        f"{stats.failed} failed | {stats.skipped_cdata + stats.skipped_empty} skipped\n"
        f"  errors          {stats.errors} ({types})\n"
        f"  dropped         {drops}\n"
        f"  api             {u.get('model') or '-'} | {u.get('requests', 0)} request(s), "
        f"{u.get('failed_requests', 0)} failed | tokens "
        f"in={u.get('prompt_tokens', 0)} out={u.get('output_tokens', 0)} "
        f"cached={u.get('cached_tokens', 0)} thinking={u.get('thinking_tokens', 0)}"
    )
    print("METRICS " + json.dumps(stats.as_dict(), default=dict))


async def _run(files: list[Path], proofreader: llm.Proofreader, batch_size: int) -> int:
    code = EXIT_OK
    t0 = time.perf_counter()
    for f in files:
        proofreader.usage = llm.Usage()
        try:
            stats = await pipeline.proofread_file(f, proofreader, batch_size)
        except (OSError, etree.XMLSyntaxError) as e:
            log.error("%s: %s", f, e)
            code = EXIT_FATAL
            continue
        stats.usage = asdict(proofreader.usage)
        _print_summary(stats)
        if stats.failed and code == EXIT_OK:
            code = EXIT_PARTIAL
    total = {"files": len(files), "seconds": round(time.perf_counter() - t0, 3),
             "peak_rss_mb": round(pipeline.peak_rss_mb() or 0, 1) or None, "exit_code": code}
    print("METRICS_TOTAL " + json.dumps(total))
    return code


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    load_dotenv()
    key = llm.api_key_from_env()
    if not key:
        log.error("GEMINI_API_KEY is not set (export it or put it in .env)")
        return EXIT_FATAL
    try:
        guide = load_style_guide(args.style_guide)
    except StyleGuideError as e:
        log.error("%s", e)
        return EXIT_FATAL
    log.info("model=%s thinking=%s lang=%s style guide=%s (%d chars)",
             args.model, args.thinking, args.lang, args.style_guide, len(guide))

    proofreader = llm.Proofreader(
        api_key=key, style_guide=guide, lang=args.lang, model=args.model,
        concurrency=args.concurrency, rpm=args.rpm, thinking=args.thinking,
    )
    try:
        return asyncio.run(_run(args.files, proofreader, args.batch_size))
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
