# SPDX-License-Identifier: Apache-2.0
"""Token-budgeted payloads for LLM agents.

Agents pay for every byte a tool returns, and they pay for it again on every
later turn while it stays in context. The legacy ``--json`` shape was built
for humans and debugging: pretty-printed, absolute paths, and the same
anchor/ranking facts repeated in ``evidence_bundle``, ``why_ranked`` and
``supporting_chunks``. Measured on Django, roughly half of an ``--agent-fast``
payload was indentation and duplicated keys.

This module owns the compact agent contract:

* :func:`compact_results` — same result keys agents already read
  (``path``, ``start_line``, ``end_line``, ``score``, ``confidence``,
  ``evidence_terms``, ``agent_summary``, ``strict_verification``), minus the
  duplicated blocks, with project-relative paths and term-focused snippets.
* :func:`outline_files` — a query-ranked declaration inventory for a few
  candidate files (``skygrep symbols``). Large modules hide the relevant
  function thousands of lines below the imports; an outline locates it for a
  few hundred tokens so the agent can read only that range.
* :func:`dumps` — JSON without whitespace.

Everything here is deterministic and model-free.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .hybrid import extract_query_terms

# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------


def dumps(payload: Any) -> str:
    """Compact JSON: no indentation, no spaces after separators."""

    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def display_path(path: str, root: Path | str | None) -> str:
    """Return ``path`` relative to ``root`` when it lives under it."""

    if not path:
        return path
    if root is None:
        return path
    try:
        p = Path(path)
        if not p.is_absolute():
            return path
        return os.path.relpath(p, Path(root)) if _is_under(p, Path(root)) else path
    except (ValueError, OSError):
        return path


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


# --------------------------------------------------------------------------
# Query terms
# --------------------------------------------------------------------------

# Question scaffolding that never names code. Kept deliberately generic:
# these are words about the *act of asking*, not about any repository.
_META_WORDS = frozenset(
    "about after also anchor anchors answer before between code could does "
    "file files from function functions have identify identifies implement "
    "implementation implemented implements into method methods name names "
    "only other prove proves should show shows source symbol symbols than "
    "their them then there these they this those used uses using what when "
    "where which while with would your".split()
)


def term_groups(query: str, *, root: Path | str | None = None, max_terms: int = 12) -> list[list[str]]:
    """One group per query concept: the term plus cheap stems.

    ``validators`` also matches ``validator``; ``rendering`` also matches
    ``render``. Grouping lets callers count *distinct concepts* a line hits
    rather than variants of one word. The repository's own name is dropped
    because it matches nearly every file (``cobra`` in the Cobra repo).
    """

    noise: set[str] = set()
    if root is not None:
        name = Path(root).name.lower()
        noise = {name, *[p for p in re.split(r"[-_.]+", name) if len(p) > 2]}
    groups: list[list[str]] = []
    seen: set[str] = set()
    for term in extract_query_terms(query, max_terms=max_terms * 2):
        if term in _META_WORDS or term in noise or term in seen:
            continue
        variants = [term]
        variants += [p for p in re.split(r"[-_]", term) if len(p) >= 4 and p != term]
        if len(term) > 5 and term.endswith("ies"):
            variants.append(term[:-3] + "y")
        elif len(term) > 4 and term.endswith("s") and not term.endswith("ss"):
            variants.append(term[:-1])
        if len(term) > 6 and term.endswith("ing"):
            variants.append(term[:-3])
        if len(term) > 5 and term.endswith("ed"):
            variants.append(term[:-2])
        group = [v for v in dict.fromkeys(variants) if v not in seen]
        seen.update(group)
        if group:
            groups.append(group)
        if len(groups) >= max_terms:
            break
    return groups


def query_terms(query: str, *, root: Path | str | None = None, max_terms: int = 12) -> list[str]:
    """Flat list of every term variant from :func:`term_groups`."""

    return [t for g in term_groups(query, root=root, max_terms=max_terms) for t in g]


# --------------------------------------------------------------------------
# Declaration outline
# --------------------------------------------------------------------------

_DECLARATION_RE = re.compile(
    r"^\s*(?:(?:export|default|pub(?:\([^)]*\))?|public|protected|private|internal|"
    r"static|final|abstract|async|override|open|unsafe|const)\s+)*"
    r"(?:def|class|func|function|fn|struct|enum|trait|interface|type|impl|mod|"
    r"module|object|record|macro_rules!)\b",
    re.IGNORECASE,
)
_JAVA_METHOD_RE = re.compile(
    r"^\s*(?:public|protected|private)\s+(?:@\w+(?:\([^)]*\))?\s+)*"
    r"[\w<>,?.\[\]]+\s+[A-Za-z_$][\w$]*\s*\("
)
_JS_BINDING_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*"
    r"(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"
)
# Imports mention every concept a module touches; they cost budget without
# locating anything, so outlines skip them.
_IMPORT_RE = re.compile(
    r"^\s*(?:import\s|from\s+\S+\s+import\s|use\s|#\s*include\b|using\s|package\s|"
    r"(?:const|let|var)\s+[\w${}\s,]+=\s*require\()"
)
_CONTROL_FLOW = (
    "if ", "if(", "for ", "for(", "while ", "while(", "switch ", "switch(",
    "return ", "else", "catch", "match ", "case ", "try", "throw ",
)

SOURCE_SUFFIXES = frozenset(
    ".c .cc .cpp .cs .go .h .hpp .java .js .jsx .mjs .cjs .kt .kts .m .mm .php "
    ".py .rb .rs .scala .swift .ts .tsx .mts .cts .lua .ex .exs .erl .hs .ml "
    ".dart .jl .r .sql".split()
)

MAX_OUTLINE_LINE_CHARS = 200
# Non-declaration lines enter an outline only when they mention at least
# this many distinct query concepts, and at most MAX_MENTION_LINES of them.
MIN_MENTION_CONCEPTS = 2
MAX_MENTION_LINES = 12
# ``expand``: how much of a top declaration's body to inline. Reading the
# best-matching range is the agent's next step anyway; doing it inside the
# outline call saves a round trip and a whole-file read.
EXPAND_LINES = 30
EXPAND_CHARS = 2400


def declaration_priority(line: str) -> int:
    """2 = definite declaration, 1 = signature-shaped line, 0 = neither."""

    stripped = line.strip()
    if not stripped:
        return 0
    if _DECLARATION_RE.search(line) or _JAVA_METHOD_RE.search(line) or _JS_BINDING_RE.search(line):
        return 2
    lowered = stripped.lower()
    if lowered.startswith(_CONTROL_FLOW) or stripped.startswith(("//", "#", "*", "/*")):
        return 0
    head = stripped.split("(", 1)[0]
    return int(
        "(" in stripped
        and ")" in stripped
        and len(stripped) <= 300
        and (stripped.endswith("{") or stripped.endswith(";") or " throws " in lowered)
        and "=" not in head
        and "." not in head.strip().split(" ")[-1]
    )


def outline_file(
    path: Path,
    terms: Iterable[str] | Iterable[list[str]],
    *,
    budget_chars: int = 4000,
    expand: int = 0,
) -> dict[str, Any]:
    """Select the most query-relevant declaration lines of ``path``.

    ``terms`` is a list of concept groups (from :func:`term_groups`) or a
    flat list of terms. Declarations are ranked by (declaration strength +
    query hit, distinct concepts hit, position) and kept until
    ``budget_chars`` is spent; the kept lines are returned in file order so
    the outline reads like the file. Imports are skipped, and plain lines
    get in only when they mention several distinct query concepts, so the
    outline stays an outline rather than a grep dump.

    ``expand`` > 0 also returns the first ``EXPAND_LINES`` lines of that many
    declarations that hit the most distinct query concepts, as ``bodies``.
    """

    groups = [g if isinstance(g, (list, tuple)) else [g] for g in terms]
    groups = [[t for t in g if t] for g in groups if g]
    try:
        text = path.read_text(errors="ignore")
    except OSError as exc:
        return {"lines": [], "total_lines": 0, "declarations": 0, "error": str(exc)}
    lines = text.splitlines()
    ranked: list[tuple[int, int, int, str]] = []
    declarations = 0
    for number, line in enumerate(lines, start=1):
        if _IMPORT_RE.match(line):
            continue
        lowered = line.lower()
        hits = sum(1 for g in groups if any(t in lowered for t in g))
        prio = declaration_priority(line)
        if prio:
            declarations += 1
        if prio == 0 and hits < MIN_MENTION_CONCEPTS:
            continue
        # Declarations always outrank plain mentions; a query hit lifts a
        # declaration above declarations that share no query term.
        score = (prio * 2) + (1 if hits else 0)
        ranked.append((-score, -hits, number, line.rstrip()))
    ranked.sort()
    kept: list[tuple[int, str]] = []
    used = 0
    mentions = 0
    for neg_score, _, number, line in ranked:
        if -neg_score <= 1:
            if mentions >= MAX_MENTION_LINES:
                continue
            mentions += 1
        rendered = line.strip()
        if len(rendered) > MAX_OUTLINE_LINE_CHARS:
            rendered = rendered[: MAX_OUTLINE_LINE_CHARS - 1] + "…"
        cost = len(rendered) + len(str(number)) + 2
        if used + cost > budget_chars:
            continue
        kept.append((number, rendered))
        used += cost
    kept.sort()
    result: dict[str, Any] = {
        "lines": kept,
        "total_lines": len(lines),
        "declarations": declarations,
        "truncated": len(kept) < len(ranked),
    }
    if expand > 0:
        def body_concepts(number: int) -> int:
            body = "\n".join(lines[number - 1 : number - 1 + EXPAND_LINES]).lower()
            return sum(1 for g in groups if any(t in body for t in g))

        # Most concepts on the declaration line first; ties go to the
        # declaration whose body covers more of the query.
        best = sorted(
            (item for item in ranked if -item[0] >= 4 and -item[1] > 0),
            key=lambda item: (item[1], -body_concepts(item[2]), item[0], item[2]),
        )[:expand]
        bodies = []
        for _, _, number, _ in sorted(best, key=lambda item: item[2]):
            chunk = lines[number - 1 : number - 1 + EXPAND_LINES]
            text = "\n".join(chunk)
            if len(text) > EXPAND_CHARS:
                text = text[: EXPAND_CHARS - 1] + "…"
            bodies.append((number, number + len(chunk) - 1, text))
        result["bodies"] = bodies
    return result


def outline_files(
    paths: Iterable[str | Path],
    query: str,
    *,
    root: Path | str | None = None,
    budget_chars: int = 4000,
    max_files: int = 3,
    expand: int = 0,
) -> list[dict[str, Any]]:
    """Outline up to ``max_files`` source files for ``query``."""

    root_path = Path(root) if root is not None else None
    terms = term_groups(query, root=root_path)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in paths:
        if len(out) >= max_files:
            break
        p = Path(raw)
        if not p.is_absolute() and root_path is not None:
            p = root_path / p
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if p.suffix.lower() not in SOURCE_SUFFIXES or not p.is_file():
            continue
        result = outline_file(p, terms, budget_chars=budget_chars, expand=expand)
        result["path"] = display_path(str(p), root_path)
        out.append(result)
    return out


def render_outline_text(outlines: list[dict[str, Any]]) -> str:
    """Plain-text rendering: one header per file, ``line: declaration`` rows."""

    blocks: list[str] = []
    for item in outlines:
        header = (
            f"## {item['path']} ({len(item['lines'])} lines shown; "
            f"{item.get('declarations', 0)} declarations in {item.get('total_lines', 0)} lines"
        )
        header += "; raise --budget for more)" if item.get("truncated") else ")"
        rows = [f"{n}: {line}" for n, line in item["lines"]]
        for start, end, text in item.get("bodies", []):
            rows.append(f"── lines {start}-{end} ──\n{text}\n──")
        blocks.append("\n".join([header, *rows]))
    return "\n\n".join(blocks)


def outline_payload(outlines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """JSON-ready outline: ``{"path", "lines": ["12: def foo(...)", ...]}``."""

    return [
        {
            "path": item["path"],
            "lines": [f"{n}: {line}" for n, line in item["lines"]],
            **({"truncated": True} if item.get("truncated") else {}),
            **(
                {"bodies": [{"start": a, "end": b, "text": t} for a, b, t in item["bodies"]]}
                if item.get("bodies")
                else {}
            ),
        }
        for item in outlines
    ]


# --------------------------------------------------------------------------
# Compact search results
# --------------------------------------------------------------------------

_SNIPPET_HEADER_RE = re.compile(r"^\[file: [^\]]*\](?: \[[^\]]*\])*\s*$")
DEFAULT_SNIPPET_BUDGET = 1200


def trim_snippet(snippet: str, terms: Iterable[str], budget_chars: int = DEFAULT_SNIPPET_BUDGET) -> str:
    """Keep the chunk's declaration and the lines that mention query terms.

    The indexer prefixes chunks with ``[file: …] [lang: …] [symbol: …]``;
    the path and language are already in the result, so only the symbol
    survives. The first code line (usually the declaration) is always kept,
    then lines with query hits plus one line of context each, in order,
    with ``⋯`` marking elided runs. Short snippets pass through unchanged.
    """

    if not snippet or len(snippet) <= budget_chars:
        return _strip_chunk_header(snippet)
    body = _strip_chunk_header(snippet)
    lines = body.splitlines()
    terms = [t for t in terms if t]
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if line.strip():
            keep.add(i)
            break
    for i, line in enumerate(lines):
        lowered = line.lower()
        if any(t in lowered for t in terms) or declaration_priority(line) == 2:
            keep.update({i - 1, i, i + 1})
    out: list[str] = []
    used = 0
    last = -2
    for i in sorted(k for k in keep if 0 <= k < len(lines)):
        line = lines[i]
        piece = line if i == last + 1 else f"⋯\n{line}" if out else line
        if used + len(piece) + 1 > budget_chars:
            break
        out.append(piece)
        used += len(piece) + 1
        last = i
    if last < len(lines) - 1:
        out.append("⋯")
    return "\n".join(out)


def _strip_chunk_header(snippet: str) -> str:
    if not snippet:
        return snippet
    first, sep, rest = snippet.partition("\n")
    if _SNIPPET_HEADER_RE.match(first.strip()):
        symbol = re.search(r"\[symbol: ([^\]]+)\]", first)
        rest = rest.lstrip("\n")
        return f"[symbol: {symbol.group(1)}]\n{rest}" if symbol else rest
    return snippet


def _compact_summary(summary: dict[str, Any], root: Path | None, shown: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("quality", "confidence", "missing_signal"):
        if key in summary:
            out[key] = summary[key]
    likely = [display_path(p, root) for p in summary.get("likely_files", []) or []]
    others = [p for p in likely if p not in shown]
    if others:
        out["likely_files"] = others[:8]
    probe = summary.get("suggested_followup_probe")
    if probe:
        out["suggested_followup_probe"] = probe.replace(f"{root}{os.sep}", "") if root else probe
    return out


def compact_results(
    results: list[dict[str, Any]],
    *,
    root: Path | str | None = None,
    include_snippet: bool = True,
    query: str = "",
    snippet_budget: int = DEFAULT_SNIPPET_BUDGET,
) -> list[dict[str, Any]]:
    """Agent-facing result list with the legacy keys and no duplication."""

    root_path = Path(root) if root is not None else None
    terms = query_terms(query, root=root_path) if query else []
    shown = {display_path(str(r.get("path", "")), root_path) for r in results}
    out: list[dict[str, Any]] = []
    for r in results:
        path = display_path(str(r.get("path", "")), root_path)
        item: dict[str, Any] = {
            "path": path,
            "start_line": r.get("start_line"),
            "end_line": r.get("end_line"),
            "score": round(float(r.get("score") or 0.0), 3),
        }
        if "confidence" in r:
            item["confidence"] = r["confidence"]
        if r.get("source_type") and r["source_type"] != "source":
            item["source_type"] = r["source_type"]
        if r.get("evidence_terms"):
            item["evidence_terms"] = r["evidence_terms"]
        lanes = r.get("candidate_recall_lanes") or (r.get("why_ranked") or {}).get("lanes")
        if lanes:
            item["lanes"] = lanes
        support = r.get("supporting_chunks") or []
        if support:
            item["also"] = [
                [c.get("start_line"), c.get("end_line")]
                if display_path(str(c.get("path", "")), root_path) == path
                else [display_path(str(c.get("path", "")), root_path), c.get("start_line"), c.get("end_line")]
                for c in support
            ]
        if include_snippet and r.get("snippet"):
            item["snippet"] = trim_snippet(str(r["snippet"]), terms, snippet_budget)
        for key in ("filename_token", "extraction_note", "strict_verification"):
            if key in r:
                item[key] = r[key]
        if isinstance(r.get("agent_summary"), dict):
            item["agent_summary"] = _compact_summary(r["agent_summary"], root_path, shown)
        out.append(item)
    return out
