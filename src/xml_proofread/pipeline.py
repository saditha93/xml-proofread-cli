"""Per-file pipeline: parse -> collect <p> -> batch -> LLM -> inject -> verify -> write."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import xmlio

log = logging.getLogger(__name__)

try:
    import resource  # not available on Windows
except ImportError:  # pragma: no cover
    resource = None


def peak_rss_mb() -> float | None:
    """Peak resident memory of this process in MB (None where unsupported)."""
    if resource is None:
        return None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KB, macOS reports bytes.
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


class Proofreader(Protocol):
    usage: object

    async def proofread(self, paragraphs: list[tuple[int, str]]) -> dict[int, list]: ...


def output_path(path: str | Path) -> Path:
    p = Path(path)
    return p.with_name(f"{p.stem}.corrected{p.suffix}")


@dataclass
class FileStats:
    input: str
    output: str = ""
    seconds: float = 0.0
    peak_rss_mb: float | None = None
    paragraphs: int = 0
    changed: int = 0
    clean: int = 0
    failed: int = 0
    skipped_cdata: int = 0
    skipped_empty: int = 0
    errors_by_type: Counter = field(default_factory=Counter)
    dropped: Counter = field(default_factory=Counter)
    usage: dict = field(default_factory=dict)

    @property
    def errors(self) -> int:
        return sum(self.errors_by_type.values())

    def as_dict(self) -> dict:
        # asdict() would rebuild Counters from (key, value) tuples, so copy them as plain dicts.
        d = {k: (dict(v) if isinstance(v, Counter) else v) for k, v in vars(self).items()}
        d["errors"] = self.errors
        d["seconds"] = round(self.seconds, 3)
        if self.peak_rss_mb is not None:
            d["peak_rss_mb"] = round(self.peak_rss_mb, 1)
        return d


async def proofread_file(path: str | Path, proofreader: Proofreader, batch_size: int = 15) -> FileStats:
    """Proofread one XML file and write <name>.corrected.xml next to it.

    Raises OSError / lxml XMLSyntaxError for unreadable or invalid input.
    """
    t0 = time.perf_counter()
    stats = FileStats(input=str(path))
    doc = xmlio.load(path)
    paras = xmlio.find_paragraphs(doc)
    stats.paragraphs = len(paras)

    todo: list[tuple[int, str]] = []
    for i, p in enumerate(paras):
        if p.has_cdata:
            stats.skipped_cdata += 1
            log.warning("paragraph %d contains CDATA; skipped", i)
        elif not p.text.strip():
            stats.skipped_empty += 1
        else:
            todo.append((i, p.text))

    batches = [todo[k:k + batch_size] for k in range(0, len(todo), batch_size)]
    log.info("%s: %d paragraph(s) to proofread in %d request(s)", path, len(todo), len(batches))
    found: dict[int, list] = {}
    for part in await asyncio.gather(*(proofreader.proofread(b) for b in batches)):
        found.update(part)

    for i, _ in todo:
        if i not in found:
            stats.failed += 1
            continue
        try:
            res = xmlio.annotate(paras[i], found[i])
        except xmlio.InvariantError as e:
            stats.failed += 1
            log.error("paragraph %d: %s", i, e)
            continue
        stats.dropped.update(res.drops)
        for cause, n in res.drops.items():
            log.debug("paragraph %d: dropped %d suggestion(s): %s", i, n, cause)
        if res.applied:
            stats.changed += 1
            stats.errors_by_type.update(x.type for x in res.applied)
        else:
            stats.clean += 1

    out = output_path(path)
    xmlio.save(doc, out)
    stats.output = str(out)
    stats.seconds = time.perf_counter() - t0
    stats.peak_rss_mb = peak_rss_mb()
    return stats
