"""Lexical (ripgrep) prefilter — the high-recall first stage of search.

Empirically on the repo-A 16-task benchmark, ripgrep with simple term extraction
hits 16/16 recall in ~0.4 s, while our pre-existing semantic-only pipeline
(cosine + cross-encoder rerank, no lexical prefilter) tops out at 14/16 in
~50 s. The right architecture for local code search is therefore:

  1. lexical_candidate_paths(query, root)  → small candidate file set    (~0.4 s)
  2. cosine + (optional) small rerank      → top-k chunks within those    (~1 s)
  3. token-compressed snippet output       → ~67 K tokens for an LLM agent

This module owns step 1: extract literal-token terms from a natural-language
query and ask ripgrep for files containing them. Files become a candidate set
that ``storage.search`` can restrict its chunk scan to. When ripgrep returns
fewer than ``min_candidates`` files, the caller should fall back to corpus-
wide cosine retrieval — for queries with no usable surface-level overlap, the
lexical layer has no signal and we must lean on the embedder.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


# Common English stopwords plus question words. Matches the heuristic the
# parity_vs_ripgrep benchmark uses, so behaviour at search time and at
# benchmark time stays in sync.
_STOPWORDS = frozenset(
    "the a an is for to of and that this where what how does in on with from "
    "are be can do not it its as by at or but all any if when which who whose".split()
)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def extract_query_terms(query: str, max_terms: int = 8) -> list[str]:
    """Return a deduplicated list of lowercased query tokens for lexical search.

    Filters: tokens shorter than 4 characters and the standard stopword set.
    Returns at most ``max_terms`` terms in original order so the most
    salient words (which English speakers tend to put first) win when the
    cap is hit.
    """

    out: list[str] = []
    seen: set[str] = set()
    for tok in _TOKEN_RE.findall(query):
        if len(tok) < 4:
            continue
        lo = tok.lower()
        if lo in _STOPWORDS or lo in seen:
            continue
        seen.add(lo)
        out.append(lo)
        if len(out) >= max_terms:
            break
    return out


def lexical_candidate_paths(
    query: str,
    root: Path,
    *,
    max_terms: int = 8,
    timeout: float = 10.0,
    rg_bin: str | None = None,
) -> set[str]:
    """Return the set of absolute file paths under ``root`` containing any
    extracted query term, using ripgrep.

    Returns the empty set when ripgrep is unavailable, when ``root`` does
    not exist, or when the query has no usable lexical terms — the caller
    treats an empty set as the signal to fall back to corpus-wide cosine.

    ripgrep is invoked once per term with ``-il -F`` (case-insensitive,
    list filenames only, fixed string). Term invocations are independent
    so this is the simplest correct path; a single regex-OR call is faster
    but loses the per-term hit attribution we may want later.
    """

    rg = rg_bin or shutil.which("rg")
    if rg is None:
        logger.warning("ripgrep not found on PATH; lexical prefilter disabled")
        return set()
    if not root.exists():
        logger.warning("lexical prefilter root does not exist: %s", root)
        return set()
    terms = extract_query_terms(query, max_terms=max_terms)
    if not terms:
        return set()
    out: set[str] = set()
    for term in terms:
        try:
            proc = subprocess.run(
                [rg, "-il", "-F", term, str(root)],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.warning("rg failed for term %r: %s", term, exc)
            continue
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line:
                out.add(line)
    return out
