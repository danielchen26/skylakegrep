"""Command-line entry point for ``skygrep``.

Two-mode CLI:

  - **Bare-form** (95% of use): ``skygrep "<query>"`` runs a search with smart
    defaults. The first argument is treated as a query whenever it isn't a
    known subcommand. Auto-index runs the first time a project is queried,
    incremental refresh runs on subsequent queries when files have changed.
  - **Subcommand form** (admin / power use): ``skygrep <verb> [args]`` for
    ``index``, ``watch``, ``serve``, ``stats``, ``doctor``, and the explicit
    ``search`` form. ``skygrep --help`` prints the full surface.

Routing rule: known subcommands win. Anything that isn't a known subcommand
is treated as the first argument to ``search``. A query that happens to
collide with a subcommand name (rare) can be quoted: ``skygrep "stats and
metrics"`` searches; ``skygrep stats`` runs the subcommand.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import click

logger = logging.getLogger(__name__)

from . import __version__
from . import auto_index, bootstrap, code_graph, config as cfg_mod, enrich as enrich_mod, integrations as integrations_mod, ui as ui_mod
from .answerer import get_answerer
from .config import get_config
from .embeddings import get_embedder
from .indexer import (
    collect_indexable_files,
    embed_file_chunks_batched,
)
from .intent import classify_intent, merge_results as merge_tiers
from .llm_router import RouterDecision, route_query, simplify_router_query
from .metadata_search import (
    analyze_metadata_query,
    descriptor_file_results,
    metadata_results,
    rank_results_by_metadata,
)
from .query_scope import resolve_scope_facet, strip_scope_clauses
from .intelligent_cli import (
    assess_result_quality,
    detect_out_of_scope,
    hints_disabled,
    mark_first_run_nudge_shown,
    render_first_run_nudge,
    render_out_of_scope_hint,
    should_show_first_run_nudge,
    suggest_for_unknown_option,
)
from .recovery import (
    get_recovery_state,
    maybe_start_recovery,
    render_recovery_footer,
)
from .render import render_compact_source, render_terminal_result
from .storage import (
    CASCADE_DEFAULT_TAU,
    cascade_search,
    delete_file_chunks,
    delete_missing_files,
    get_indexed_files,
    init_db,
    path_matches,
    populate_file_embeddings,
    populate_symbols,
    search,
    store_chunks_batch,
)


def _symbols_table_populated(conn) -> bool:
    """Return True iff the ``symbols`` table has at least one row.

    Wrapped because the table may be missing on databases built before L2.
    The init_db pass adds the table, but the underlying sqlite query is
    cheap enough that a try/except is the simplest contract.
    """

    try:
        row = conn.execute("SELECT EXISTS (SELECT 1 FROM symbols LIMIT 1)").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


def _ui_step(label: str, message: str) -> str:
    return ui_mod.step(label, message)


def _ui_detail(message: str) -> str:
    return ui_mod.detail(message)


def _ui_done(elapsed: float, quality: str) -> str:
    return ui_mod.done(elapsed, quality)


def _ui_rows(rows: list[tuple[str, str]]) -> str:
    return ui_mod.rows(rows)


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


def _apply_foreground_model_timeout(obj, seconds: float) -> None:
    """Best-effort timeout contract for Ollama-backed foreground calls."""

    try:
        setattr(obj, "request_timeout_s", seconds)
        setattr(obj, "batch_timeout_s", seconds)
        setattr(obj, "allow_per_chunk_fallback", False)
    except Exception:
        pass


def _setup_auto_refresh_enabled() -> bool:
    """Whether normal commands may refresh existing managed setup blocks."""

    value = os.environ.get("SKYGREP_SETUP_AUTO_REFRESH", "1").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    # Tests and CI should not mutate a developer's real agent config files.
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("CI"):
        return False
    return True


def _auto_refresh_setup_snippets() -> list[integrations_mod.Integration]:
    """Best-effort refresh of already-registered LLM-agent snippets."""

    if not _setup_auto_refresh_enabled():
        return []
    try:
        return integrations_mod.refresh_registered_snippets()
    except Exception:
        logger.exception("setup snippet auto-refresh failed; ignoring")
        return []


def merge_results(result_groups: list[list[dict]], top: int) -> list[dict]:
    merged = {}
    for results in result_groups:
        for result in results:
            key = (
                result["path"],
                result.get("start_line"),
                result.get("end_line"),
                result["snippet"],
            )
            if key not in merged or result["score"] > merged[key]["score"]:
                merged[key] = result
    return sorted(merged.values(), key=lambda item: item["score"], reverse=True)[:top]


def _is_filename_lookup_result(result: dict | None) -> bool:
    return bool(result and result.get("fallback") == "filename-lookup")


def _result_is_depth_upgrade(existing: dict | None, candidate: dict | None) -> bool:
    """Whether ``candidate`` should replace an already-rendered same-path hit.

    Path-level anchors are useful early, but a later semantic/lazy result
    for the same file contains the real answer body. Prefer that deeper
    evidence while preserving the first path position.
    """

    if not candidate:
        return False
    if not existing:
        return True
    if _is_filename_lookup_result(existing) and not _is_filename_lookup_result(candidate):
        return bool((candidate.get("snippet") or candidate.get("chunk") or "").strip())
    return False


def _merge_sources_preferring_depth(
    sources: tuple[list[dict], ...],
    *,
    top: int,
) -> list[dict]:
    """Merge path-ranked result tiers while upgrading anchors to content.

    The cold-start pipeline can find the same file twice: first as a fast
    filename anchor, then as a lazy semantic chunk. The old path-only
    dedupe kept the anchor metadata and discarded the semantic body. This
    helper keeps the anchor's position but swaps in the deeper result.
    """

    ordered: list[dict] = []
    by_path: dict[str, int] = {}
    for source in sources:
        for result in source:
            path = result.get("path", "")
            if not path:
                continue
            if path in by_path:
                idx = by_path[path]
                if _result_is_depth_upgrade(ordered[idx], result):
                    ordered[idx] = result
                continue
            if len(ordered) >= top:
                continue
            by_path[path] = len(ordered)
            ordered.append(result)
    return ordered[:top]


def _semantic_filename_anchor_should_lead(
    decision: "RouterDecision | None",
    fn_results: list[dict],
) -> bool:
    """Whether concrete filename anchors should scope a semantic query.

    A semantic query with a real basename hit is different from a broad
    semantic query. The user is asking *about that artifact*, so the
    filename anchor must remain the first evidence tier and later semantic
    chunks may upgrade it. This prevents unrelated high-scoring cascade hits
    from pushing a requested PDF/DOCX/code file out of top-K.
    """

    return bool(
        fn_results
        and decision is not None
        and getattr(decision, "intent", "") == "semantic"
    )


def _apply_adaptive_metadata_ranking(
    results: list[dict],
    decision: "RouterDecision | None",
) -> list[dict]:
    """Use metadata facets as cheap rerankers for composite searches.

    A non-terminal metadata facet means the query contains a timestamp/size
    constraint plus a real target/content ask. Retrieval still supplies the
    candidate set; the metadata facet only breaks ties inside that relevant
    set, so accuracy improves without adding a filesystem scan.
    """

    if not results or decision is None:
        return results
    kind = getattr(decision, "metadata_kind", None)
    if not kind or getattr(decision, "metadata_terminal", False):
        return results
    return rank_results_by_metadata(results, kind)


def _suppress_nonterminal_out_of_scope_for_scope(
    query: str,
    decision: "RouterDecision | None",
    *,
    explicit_scope: bool,
) -> None:
    """Keep metadata as a facet when a scoped query still asks for content.

    The LLM may see a temporal-looking word inside a scoped content question
    and mark it as a metadata-only request. Scope plus remaining target terms
    means that is not safe: terminal metadata is still handled by
    ``metadata_results`` before routing, while non-terminal or unproven
    metadata must let normal retrieval continue.
    """

    if not explicit_scope or decision is None:
        return
    if getattr(decision, "out_of_scope", None) not in {"recency", "size", "listing"}:
        return
    facet = analyze_metadata_query(query)
    if facet is not None and facet.terminal:
        return
    decision.out_of_scope = "none"


def _filter_results_to_explicit_scope(
    results: list[dict],
    scope_root: Path | None,
) -> list[dict]:
    """Keep a query-plan scope authoritative across every retrieval lane.

    Cross-folder and proactive lanes may have embedded external files into a
    scoped DB during earlier versions. Once the current query has an explicit
    filesystem scope, those old rows must not leak back into ranking.
    """

    if scope_root is None or not results:
        return results
    try:
        resolved_scope = scope_root.expanduser().resolve()
    except OSError:
        return results
    scoped: list[dict] = []
    for result in results:
        raw_path = result.get("path") or result.get("file") or ""
        if not raw_path:
            continue
        path = Path(str(raw_path)).expanduser()
        if not path.is_absolute():
            if str(path).startswith(".."):
                continue
            scoped.append(result)
            continue
        try:
            if path.resolve().is_relative_to(resolved_scope):
                scoped.append(result)
        except OSError:
            continue
    return scoped


def _filter_results_to_cli_path_filters(
    results: list[dict],
    *,
    project_root: Path,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
) -> list[dict]:
    """Apply CLI include/exclude as a hard output boundary.

    Retrieval lanes such as metadata, filename, proactive, and cascade can use
    different internal path representations. Human and agent callers still
    expect ``--include`` / ``--exclude`` to be authoritative at the final
    boundary, especially for ``--json`` context.
    """

    if not results or (not include_patterns and not exclude_patterns):
        return results
    try:
        resolved_root = project_root.expanduser().resolve()
    except OSError:
        resolved_root = project_root
    filtered: list[dict] = []
    for result in results:
        raw_path = str(result.get("path") or result.get("file") or "")
        if not raw_path:
            continue
        candidates = [raw_path]
        path = Path(raw_path).expanduser()
        if path.is_absolute():
            try:
                candidates.append(path.resolve().relative_to(resolved_root).as_posix())
            except (OSError, ValueError):
                pass
        include_ok = (
            not include_patterns
            or any(path_matches(candidate, include_patterns, ()) for candidate in candidates)
        )
        excluded = any(
            not path_matches(candidate, (), exclude_patterns) for candidate in candidates
        )
        if include_ok and not excluded:
            filtered.append(result)
    return filtered


def _apply_result_boundaries(
    results: list[dict],
    *,
    project_root: Path,
    explicit_scope: bool,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
) -> list[dict]:
    results = _filter_results_to_explicit_scope(
        results,
        project_root if explicit_scope else None,
    )
    return _filter_results_to_cli_path_filters(
        results,
        project_root=project_root,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
    )


def _agent_lexical_scan_root(
    project_root: Path,
    *,
    lexical_root: str | None,
    include_patterns: tuple[str, ...],
) -> Path:
    if lexical_root:
        root = Path(lexical_root)
        return root if root.is_absolute() else project_root / root
    for pattern in include_patterns:
        head = pattern.split("*", 1)[0].rstrip("/")
        if head and not head.startswith("!"):
            root = Path(head)
            return root if root.is_absolute() else project_root / root
    return project_root


def _lexical_evidence_satisfies_depth(
    query: str,
    results: list[dict],
    decision: "RouterDecision | None",
    *,
    detail: str,
    answer: bool,
    agentic: bool,
) -> bool:
    """Return True when exact text evidence can finish the default CLI view.

    This is evidence-based, not keyword-based: the text snippet must contain
    multiple query terms and carry a strong lexical score. Synthesized/agentic
    calls keep semantic retrieval alive because they need deeper context.
    """

    if not results or answer or agentic:
        return False
    if decision is not None and getattr(decision, "intent", "") == "filename":
        return False
    if detail == "full":
        return False
    search_query = strip_scope_clauses(query) or query
    terms = [t.lower() for t in auto_index.extract_query_terms(search_query)]
    if len(terms) < 2:
        return False
    best = results[0]
    try:
        lexical_score = float(best.get("lexical_score", best.get("score", 0.0)))
    except (TypeError, ValueError):
        lexical_score = 0.0
    min_lexical_score = 0.18 if search_query != query else 0.4
    if lexical_score < min_lexical_score:
        return False
    text = f"{best.get('snippet', '')}\n{best.get('chunk', '')}".lower()
    content_hits = {
        term
        for term in terms
        if any(variant in text for variant in _term_surface_variants(term))
    }
    return len(content_hits) >= 2


def _term_surface_variants(term: str) -> set[str]:
    """Small language-agnostic-ish surface variants for lexical evidence.

    This is not stemming for retrieval ranking. It only decides whether an
    already-returned exact-text result is strong enough to avoid waiting for
    a deeper semantic refinement in the default human CLI view.
    """

    term = (term or "").lower().strip()
    if len(term) < 3:
        return {term} if term else set()
    variants = {term}
    if term.endswith("ies") and len(term) > 4:
        variants.add(term[:-3] + "y")
    if term.endswith("es") and len(term) > 4:
        variants.add(term[:-2])
    if term.endswith("s") and len(term) > 3:
        variants.add(term[:-1])
    if term.endswith("ed") and len(term) > 4:
        variants.add(term[:-2])
    if term.endswith("ing") and len(term) > 5:
        variants.add(term[:-3])
    return {v for v in variants if len(v) >= 3}


def _filter_low_evidence_machine_results(
    results: list[dict],
    query: str,
    *,
    min_score: float,
) -> list[dict]:
    """Suppress low-confidence JSON false positives for agent callers.

    Human search can use low-score semantic suggestions as exploratory
    hints. Machine callers are different: a low-score result with no
    lexical evidence is usually worse than an empty list because the agent
    may treat it as authoritative context. This gate is evidence based:
    keep results when either the semantic score is strong enough or any
    distinctive query term appears in the returned path/snippet text.
    """

    if not results:
        return results
    try:
        best_score = max(float(r.get("score", 0.0) or 0.0) for r in results)
    except (TypeError, ValueError):
        best_score = 0.0
    if best_score >= min_score:
        return results

    search_query = strip_scope_clauses(query) or query
    terms = auto_index.extract_query_terms(search_query, max_terms=8)
    if not terms:
        return results

    haystack_parts: list[str] = []
    for result in results:
        haystack_parts.extend(
            str(result.get(key, "") or "")
            for key in ("path", "snippet", "chunk", "content_excerpt", "content_preview")
        )
    haystack = "\n".join(haystack_parts).casefold()
    hits: set[str] = set()
    for term in terms:
        if any(variant in haystack for variant in _term_surface_variants(term)):
            hits.add(term)
    required_hits = 1 if len(terms) <= 2 else max(2, min(3, (len(terms) + 1) // 2))
    if len(hits) >= required_hits:
        return results
    return []


_QUERY_EDGE_QUOTES = "\"'“”‘’"


def _normalize_query_args(query: str | tuple[str, ...]) -> str:
    """Normalize Click query arguments into the user-intended query string.

    Shells only treat ASCII quotes as grouping characters. If a terminal,
    IME, or autocomplete inserts smart quotes (`“like this”`), zsh passes
    every word as a separate argv element and Click would normally report
    "unexpected extra arguments". Accept multiple query words and strip only
    quote characters that sit on the outside edge.
    """

    if isinstance(query, tuple):
        query_s = " ".join(part for part in query if part)
    else:
        query_s = query
    return query_s.strip().strip(_QUERY_EDGE_QUOTES).strip()


def _click_option_explicit(name: str) -> bool:
    """Return whether a Click option came from the command line/env.

    Click preserves the parameter source at runtime, which lets us keep
    human CLI defaults while making agent presets stricter by default.
    """

    try:
        ctx = click.get_current_context(silent=True)
        if ctx is None or not hasattr(ctx, "get_parameter_source"):
            return False
        source = ctx.get_parameter_source(name)
        if source is None:
            return False
        return getattr(source, "name", "") != "DEFAULT"
    except Exception:
        return False


def _effective_llm_router_for_agent_mode(
    agent_mode: str,
    llm_router: bool,
    *,
    llm_router_explicit: bool,
) -> bool:
    """Agent presets prioritize bounded latency over model-routed intent.

    Human CLI calls keep the default LLM router. Machine-readable agent
    presets can opt back in with explicit ``--llm-router`` when ambiguity is
    worth the extra local-model latency.
    """

    if agent_mode in {"fast", "context", "deep"} and not llm_router_explicit:
        return False
    return llm_router


def _effective_cascade_for_agent_mode(
    agent_mode: str,
    cascade: bool,
    *,
    cascade_explicit: bool,
) -> bool:
    """First-pass agent presets should not enter the slow semantic cascade."""

    if agent_mode in {"fast", "context"} and not cascade_explicit:
        return False
    return cascade


def _filename_evidence_satisfies_depth(
    query: str,
    decision: RouterDecision,
    *,
    detail: str,
    answer: bool,
    agentic: bool,
) -> bool:
    """Return True only when filename evidence can finish the foreground.

    Filename evidence is an anchor; it is final only for path-depth
    requests. For content / explanation / synthesized-answer requests,
    the semantic layer must keep running even if the basename match is
    perfect. The fast intent substrate is used as an independent veto so
    a single router misclassification cannot under-search.
    """

    if answer or agentic:
        return False
    if getattr(decision, "intent", "") != "filename":
        return False
    # Explicit full detail means the foreground can satisfy the user by
    # extracting content from the concrete filename hit at render time.
    if detail == "full":
        return True
    try:
        from .fast_intent import classify_fast_intent
        fast = classify_fast_intent(query)
    except Exception:
        fast = None
    if fast is None:
        return False
    if (
        fast.intent == "metadata"
        and getattr(decision, "intent", "") == "filename"
        and getattr(decision, "metadata_kind", None)
        and not getattr(decision, "metadata_terminal", False)
    ):
        return True
    return fast.intent == "filename"


def _descriptor_file_evidence_satisfies_depth(
    results: list[dict],
    decision: RouterDecision,
    *,
    answer: bool,
    agentic: bool,
) -> bool:
    """Return True when scoped descriptor+metadata evidence can answer now.

    Descriptor-file results are path/location evidence. They are final for
    mixed/filename location asks, but semantic/answer/agentic calls still
    keep deeper retrieval alive.
    """

    if not results or answer or agentic:
        return False
    if getattr(decision, "metadata_terminal", False):
        return False
    if not getattr(decision, "metadata_kind", None):
        return False
    return getattr(decision, "intent", "") in {"filename", "mixed"}


def _render_detail_for_result(
    result: dict,
    default_detail: str,
    decision: "RouterDecision | None",
) -> str:
    """Choose render depth for one result without changing retrieval.

    If a semantic-depth query found a concrete filename anchor, the user
    needs evidence from the file, not just path metadata. Promote those
    anchor cards to bounded full extraction in the standard view. Pure
    filename/path queries keep the normal metadata-only standard view.
    """

    if (
        default_detail == "standard"
        and decision is not None
        and getattr(decision, "intent", "") == "semantic"
        and _is_filename_lookup_result(result)
    ):
        result["_skygrep_semantic_depth"] = True
        return "full"
    if (
        decision is not None
        and getattr(decision, "intent", "") == "semantic"
        and _is_filename_lookup_result(result)
    ):
        result["_skygrep_semantic_depth"] = True
    return default_detail


def _augment_filename_content_for_machine(
    results: list[dict],
    query: str,
    decision: "RouterDecision | None",
    *,
    detail: str,
    ocr: bool,
    for_answer: bool = False,
) -> None:
    """Attach structured PDF/DOCX/TXT excerpts for machine consumers.

    Human rendering can lazily extract document text inside
    ``render_terminal_result``. JSON and ``--answer`` do not go through that
    renderer, so they need the same evidence attached directly to the result
    dict. This only fires for filename anchors when the query depth is
    semantic/answer/full-detail; pure path lookups keep the lightweight
    metadata-only shape.
    """

    semantic_depth = (
        decision is not None
        and getattr(decision, "intent", "") == "semantic"
    )
    should_extract = semantic_depth or for_answer or detail == "full"
    if not should_extract:
        return
    try:
        from . import binary_extract
    except Exception:
        return

    for result in results:
        if not _is_filename_lookup_result(result):
            continue
        raw_path = result.get("path") or result.get("file")
        if not raw_path:
            continue
        try:
            extracted = binary_extract.extract_text(Path(raw_path), ocr=ocr)
        except Exception:
            continue
        result["extracted_text_source"] = extracted.source
        if extracted.note:
            result["extraction_note"] = extracted.note
        anchor = str(result.get("filename_token") or Path(raw_path).stem)
        excerpts = binary_extract.query_focused_passages(
            extracted.text,
            query,
            anchor=anchor,
            max_passages=1 if detail == "summary" else 2,
            max_chars=220 if detail == "summary" else 900,
        )
        if excerpts:
            result["query_excerpts"] = excerpts
            result["content_excerpt"] = "\n\n".join(excerpts)
        if detail == "full":
            preview, was_truncated = binary_extract.truncate(
                extracted.text, 1200,
            )
            if preview:
                result["content_preview"] = preview
                result["content_preview_truncated"] = was_truncated
        if for_answer and excerpts:
            metadata = result.get("snippet") or result.get("chunk") or ""
            result["snippet"] = (
                f"{metadata}\n\nRelevant excerpts:\n"
                + "\n\n".join(excerpts)
            ).strip()


def _build_explain_string(r: dict, decision: "RouterDecision | None") -> str:
    """Build a one-line 'why this hit' string from signals already on
    the result dict. Read-only — no extra retrieval, no model calls.

    Channels considered (in priority order):
      - filename-lookup        (r["fallback"] == "filename-lookup")
      - rg-shortcut / ripgrep  (lexical fast path)
      - cosine + symbol RRF    (cosine_rank + symbol_rank both set)
      - cosine cascade         (cosine_rank only)
      - symbol channel only    (symbol_rank only)

    Adds, when present: matched symbol terms (with exact/fuzzy marker),
    the LLM router's primary_token (for filename hits), and the cosine
    score / RRF fused score.
    """
    parts: list[str] = []
    fb = (r.get("fallback") or "").strip()

    if fb == "filename-lookup":
        parts.append("filename-lookup")
        token = r.get("filename_token") or (
            decision.primary_token if decision is not None else ""
        )
        if token:
            parts.append(f'token "{token}"')
    elif fb in ("ripgrep", "rg-shortcut"):
        parts.append(fb)
        ls = r.get("lexical_score")
        if ls is not None:
            try:
                parts.append(f"lex={float(ls):.2f}")
            except (TypeError, ValueError):
                pass
    else:
        cosine_rank = r.get("cosine_rank")
        symbol_rank = r.get("symbol_rank")
        if cosine_rank and symbol_rank:
            parts.append(f"cosine #{cosine_rank} ⊕ symbol #{symbol_rank} (RRF)")
        elif cosine_rank:
            parts.append(f"cosine #{cosine_rank}")
        elif symbol_rank:
            parts.append(f"symbol #{symbol_rank}")
        else:
            parts.append("cosine cascade")
        if r.get("symbol_channel"):
            terms = r.get("symbol_channel_terms") or []
            if isinstance(terms, list) and terms:
                marker = "exact" if r.get("symbol_channel_exact") else "fuzzy"
                parts.append(f"symbol[{','.join(str(t) for t in terms[:3])}] {marker}")

    score = r.get("score")
    if score is not None:
        try:
            parts.append(f"score={float(score):.3f}")
        except (TypeError, ValueError):
            pass
    fused = r.get("fused_score")
    if fused is not None:
        try:
            parts.append(f"rrf={float(fused):.3f}")
        except (TypeError, ValueError):
            pass

    return " · ".join(parts)


def _format_router_explain(
    decision: "RouterDecision | None",
    *,
    include_reason: bool = True,
) -> str:
    """Router status for human output.

    The first line is concise enough to show by default so users and
    agent wrappers can see the active routing path immediately. The
    reason line stays behind ``--explain`` to avoid noisy default output.
    """
    if decision is None:
        return ""
    intent = decision.intent or "?"
    head = _ui_step("route", f"router: {intent}")
    if decision.primary_token:
        head += f' · primary_token="{decision.primary_token}"'
    try:
        head += f" · conf={float(decision.confidence or 0.0):.2f}"
    except (TypeError, ValueError):
        pass
    if decision.source:
        head += f" · source={decision.source}"
    meta = getattr(decision, "metadata_kind", None)
    if meta:
        mode = "terminal" if getattr(decision, "metadata_terminal", False) else "modifier"
        head += f" · metadata={meta}:{mode}"
    reason = (decision.reason or "").strip()
    if include_reason and reason:
        head += f'\n{_ui_detail(f"reason: {reason}")}'
    return head


def _format_lane_explain(cascade_telemetry: dict | None) -> str:
    """One-line cascade-lane summary for the top of --explain output."""
    if not cascade_telemetry:
        return ""
    path = cascade_telemetry.get("path") or "?"
    out = _ui_step("cascade", f"lane: {path}")
    gap = cascade_telemetry.get("gap")
    tau = cascade_telemetry.get("tau")
    try:
        if gap is not None and tau is not None:
            out += f" (gap={float(gap):.3f}, tau={float(tau):.3f})"
    except (TypeError, ValueError):
        pass
    return out


def _attach_explain(results: list[dict], decision: "RouterDecision | None") -> None:
    """Mutate each result in place, populating r['explain']."""
    if not results:
        return
    for r in results:
        if not r.get("explain"):
            r["explain"] = _build_explain_string(r, decision)


def render_json_results(results: list[dict], *, include_snippet: bool = True) -> str:
    payload = []
    optional_keys = (
        "fallback",
        "filename_token",
        "source",
        "query_excerpts",
        "content_excerpt",
        "content_preview",
        "content_preview_truncated",
        "extracted_text_source",
        "extraction_note",
        "candidate_recall",
        "candidate_recall_lanes",
        "source_type",
        "search_intent",
        "evidence_terms",
        "why_ranked",
        "evidence_bundle",
        "supporting_chunks",
        "confidence",
        "agent_summary",
    )
    for r in results:
        item = {
            "path": r["path"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
            "language": r["language"],
            "score": float(r["score"]),
        }
        if include_snippet:
            item["snippet"] = r["snippet"]
        for key in optional_keys:
            if key in r:
                item[key] = r[key]
        payload.append(item)
    return json.dumps(payload, indent=2)


# Subcommand names that take precedence over bare-form query routing.
_SUBCOMMANDS = {"index", "search", "watch", "serve", "stats", "doctor", "enrich", "setup"}
_DETAIL_CHOICES = {"brief", "standard", "full", "summary"}
_AGENT_MODE_CHOICES = {"off", "fast", "context", "deep", "answer"}
_DEFAULT_AGENT_DAEMON_URL = "http://127.0.0.1:7878"


def _normalize_search_cli_args(args: list[str]) -> list[str]:
    """Make common human shorthand parse like the documented long form."""

    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--detail":
            next_arg = args[i + 1] if i + 1 < len(args) else None
            if next_arg in _DETAIL_CHOICES:
                out.extend([arg, next_arg])
                i += 2
            else:
                # Human shorthand: `skygrep --detail "query"` means
                # "show the detailed view for this query", not "use the
                # query string as the detail enum value".
                out.append("--detail=full")
                i += 1
            continue
        out.append(arg)
        i += 1
    return out


class MgrepCLI(click.Group):
    """Click group that routes unknown first-args to ``search``.

    Implements two adjustments to default Click behaviour:
      - ``skygrep "<query>"`` (no subcommand) routes to ``search "<query>"``.
      - ``skygrep stats and metrics`` (subcommand-shaped but with extra args
        that don't fit) prints a friendly suggestion to quote the query.
    """

    def parse_args(self, ctx, args):  # type: ignore[override]
        # Three routing cases:
        #   1. First arg is a known subcommand → parse normally.
        #   2. First arg is a top-level concern (`--help` / `-h` / `--version`
        #      with no positional after it) → parse normally so Click can
        #      render group-level help / version output.
        #   3. Everything else (bare query, OR a search-level flag like
        #      ``-x`` / ``-n`` / ``--json`` followed by a query) → treat the
        #      whole arg vector as a search invocation by prepending
        #      ``search`` so search-command flags resolve correctly.
        # 0.5.8.2: case 3 used to require ``args[0]`` to NOT start with ``-``,
        # which broke ``skygrep -x "<query>"`` — Click parsed ``-x`` at the
        # group level and reported "No such option: -x". The fix below routes
        # any non-subcommand-headed invocation through ``search``.
        if not args:
            return super().parse_args(ctx, args)
        first = args[0]
        if first == "search":
            return super().parse_args(
                ctx, ["search", *_normalize_search_cli_args(list(args[1:]))]
            )
        if first in _SUBCOMMANDS:
            return super().parse_args(ctx, args)
        if first in ("--help", "-h", "--version"):
            return super().parse_args(ctx, args)
        return super().parse_args(ctx, ["search", *_normalize_search_cli_args(list(args))])


@click.group(cls=MgrepCLI, invoke_without_command=True)
@click.version_option(__version__, prog_name="skygrep")
@click.pass_context
def cli(ctx):
    """``skygrep`` — local semantic code search.

    Common usage:

        \b
        skygrep "where is the auth token refreshed?"      # bare query
        skygrep --content --detail standard "what says rollback?"
        skygrep --detail "show the deployment steps"      # shorthand for full
        skygrep --agent-fast "where is token refresh?"    # JSON path anchors
        skygrep --agent-context "what does token refresh do?"  # JSON evidence
        skygrep doctor                                    # health check
        skygrep stats                                     # index info
        skygrep index .                                   # explicit reindex

    Information depth:

        \b
        locate:    skygrep "where is the project brief?"
        snippets:  skygrep --content --detail standard "what does X say?"
        deep read: skygrep --content --detail full --include "docs/file.md" "show steps"
        answer:    skygrep --answer --content "summarize X"
        agent path: skygrep --agent-fast "where is X?"
        agent ctx:  skygrep --agent-context --include "src/**" "what does X say?"
    """

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
@click.argument("path", default=".")
@click.option("--reset", is_flag=True, help="Reset existing index before reindexing")
@click.option("--incremental/--full", default=True, help="Only update changed files")
def index(path: str, reset: bool, incremental: bool):
    """Build or refresh the index explicitly. ``skygrep search`` already
    auto-indexes the first time you query a project; use this command for
    forced full rebuilds, ``--reset`` after switching embedding models, or
    indexing a directory other than the current working tree."""

    config = get_config()
    db_path = config["db_path"]
    if reset and db_path.exists():
        db_path.unlink()
    conn = init_db(db_path)
    embedder = get_embedder()

    root = Path(path)
    files = collect_indexable_files(root)

    click.echo(f"Found {len(files)} files to index")

    if incremental and not reset:
        indexed_files = get_indexed_files(conn)
        deleted_files = delete_missing_files(conn, {str(f) for f in files}, root)
        to_index = []
        to_reindex = []
        for f in files:
            f_str = str(f)
            if f_str not in indexed_files:
                to_index.append(f)
            elif f.stat().st_mtime > indexed_files[f_str]:
                to_reindex.append(f)
        click.echo(
            f"Incremental: {len(to_index)} new, {len(to_reindex)} changed, "
            f"{len(deleted_files)} deleted"
        )
        files_to_process = to_index + to_reindex
    else:
        files_to_process = files

    if not files_to_process:
        click.echo("No files to index")
        return

    click.echo(f"Indexing {len(files_to_process)} files...")
    total_chunks = 0
    for f, chunks in embed_file_chunks_batched(
        files_to_process,
        embedder,
        root=root,
    ):
        delete_file_chunks(conn, str(f))
        if chunks:
            store_chunks_batch(conn, chunks)
            total_chunks += len(chunks)
            click.echo(f"  Indexed: {f} ({len(chunks)} chunks)")

    click.echo(
        f"Indexing complete! {total_chunks} chunks in {len(files_to_process)} files"
    )
    file_count = populate_file_embeddings(conn)
    click.echo(
        f"File-level embeddings populated: {file_count} files (mean of chunk vectors)"
    )
    # Mark refresh state so subsequent searches skip the throttle.
    auto_index._meta_set(conn, "last_full_index_at", str(time.time()))
    auto_index._meta_set(conn, "last_refresh_at", str(time.time()))


@cli.command("search")
@click.argument("query", nargs=-1, required=True)
@click.option("--top", "-n", "-m", default=5, help="Number of results")
@click.option("--json", "json_output", is_flag=True, help="Emit stable JSON results")
@click.option("--answer", is_flag=True, help="Synthesize a local Ollama answer from search results")
@click.option("--content/--no-content", default=True, help="Show or hide matched snippets")
@click.option("--language", multiple=True, help="Restrict results to language(s)")
@click.option("--include", "include_patterns", multiple=True, help="Include only matching paths")
@click.option("--exclude", "exclude_patterns", multiple=True, help="Exclude matching paths")
@click.option("--agentic", is_flag=True, help="Use local Ollama to split the query into bounded subqueries")
@click.option("--max-subqueries", default=3, help="Maximum local agentic subqueries")
@click.option("--semantic-only", is_flag=True, help="Disable local lexical reranking and use pure vector similarity")
@click.option("--rerank/--no-rerank", default=True, help="Apply cross-encoder reranking on the non-cascade path (default on when sentence-transformers is installed)")
@click.option("--rerank-pool", default=None, type=int, help="Candidate pool size before reranking (default 50, env SKYGREP_RERANK_POOL)")
@click.option("--rerank-model", default=None, help="HuggingFace cross-encoder model id for reranking")
@click.option("--hyde/--no-hyde", default=False, help="Force a HyDE rewrite even outside the cascade (rarely needed; the cascade decides per query)")
@click.option("--multi-resolution/--no-multi-resolution", default=True, help="Two-stage retrieval: pick top-N files by file-level cosine first, then drill into their chunks (helps small canonical files compete against large consumer files)")
@click.option("--file-top", default=30, type=int, help="Number of files surfaced by file-level retrieval before chunk-level scoring (only used with --multi-resolution)")
@click.option("--lexical-prefilter/--no-lexical-prefilter", default=True, help="Use ripgrep to narrow the candidate file set before cosine + rerank (default on; the high-recall fast path)")
@click.option("--lexical-root", default=None, help="Root directory ripgrep scans for the lexical prefilter (defaults to the project root)")
@click.option("--lexical-min-candidates", default=2, type=int, help="If ripgrep returns fewer than this many candidate files we fall back to corpus-wide cosine retrieval")
@click.option("--agent-mode", default="off", type=click.Choice(sorted(_AGENT_MODE_CHOICES)), help="Preset output depth for LLM callers: fast=JSON path anchors, context=JSON snippets, deep=JSON full detail, answer=local synthesized answer.")
@click.option("--agent-fast", is_flag=True, help="Shortcut for --agent-mode fast: JSON path anchors, --no-content, --top 10, --no-rerank.")
@click.option("--agent-context", is_flag=True, help="Shortcut for --agent-mode context: JSON snippets, --content, --detail standard, --top 8, --no-rerank.")
@click.option("--agent-daemon/--no-agent-daemon", default=False, help="Daemon-first agent call: use SKYGREP_DAEMON_URL or http://127.0.0.1:7878, falling back in-process if unavailable.")
@click.option("--daemon-url", default=None, help="If set, send the search to a running skygrep daemon instead of loading the reranker in-process (eliminates cold-load latency)")
@click.option("--rank-by", default="chunk", type=click.Choice(["chunk", "file"]), help="Ranking strategy on the non-cascade path: 'chunk' returns top-K chunks with per-file diversity cap; 'file' returns one best chunk per file")
@click.option("--cascade/--no-cascade", default=True, help="Confidence-gated retrieval (default on for human CLI): cheap file-mean cosine first, escalate to HyDE-union only on uncertain queries. Agent fast/context presets default to --no-cascade for bounded first-pass latency unless --cascade is passed explicitly.")
@click.option("--cascade-tau", default=CASCADE_DEFAULT_TAU, type=float, help=f"Confidence threshold (top1 - top2 file-mean cosine) above which the cascade returns the cheap result. Default {CASCADE_DEFAULT_TAU}.")
@click.option("--auto-index/--no-auto-index", default=None, help="Auto-build the index for this project on first query and refresh on subsequent queries. Default: on for the project-scoped DB; off when SKYGREP_DB_PATH is set externally so curated indexes are not auto-mutated.")
@click.option("--rg-shortcut/--no-rg-shortcut", default=True, help="Lexical pre-gate: if the query is short and ripgrep returns a small, clustered, path-token-overlapping result set, return the rg result directly and skip the semantic cascade. Default on. Pass --no-rg-shortcut to force pure cascade (useful for benchmarking).")
@click.option("--filename-shortcut/--no-filename-shortcut", default=True, help="Filename-lookup pre-gate (v0.13.0+): when the query looks like 'where is foo file' / 'find package.json', route to `find -iname '*token*'` and skip both content shortcuts. Default on. Pass --no-filename-shortcut to disable.")
@click.option("--llm-router/--no-llm-router", default=True, help="LLM-driven query understanding (v0.15.0+). Routes human CLI queries via a small local Ollama model (default qwen2.5:3b) for generic intent classification. Agent presets default to rule-based routing for bounded latency unless --llm-router is passed explicitly. Falls back to v0.14.0 hand-rolled rules on any failure.")
@click.option("--detail", default="standard", type=click.Choice(["brief", "standard", "full", "summary"]), help="Output verbosity. `brief` = path + score one-liner. `standard` = +10 lines body (default). `full` = +full extracted PDF/docx content for filename matches. `summary` = +1-line truncated preview (first non-empty line, ≤160 chars; no LLM call). Bare `--detail \"query\"` is accepted as shorthand for `--detail full \"query\"`.")
@click.option("--ocr", is_flag=True, help="Run tesseract OCR on scanned PDFs (slow, ~5-30s/page). Opt-in only; requires tesseract + pdftoppm on PATH.")
@click.option("--lazy/--no-lazy", default=True, help="0.5.1+ auto-trigger: on cold-start (no index yet), if ripgrep alone returns a weak result (few hits or no path/token overlap with the query) the cold-start path also fires the lazy LLM-routed semantic tier (~5 s) and merges. When ripgrep already returns a strong keyword answer, lazy is skipped — user gets the instant rg result. Default on so the user never has to know which tier they need; pass --no-lazy to force pure rg cold-start (benchmarking).")
@click.option("--explain", "-x", is_flag=True, help="0.5.8+ explainability: print one-line 'why this hit' rationale per result (which channel, score, matched symbol) plus a top-of-output router decision and cascade lane. Uses signals already in the pipeline — no extra retrieval, no model calls. Default off (existing UX is unchanged).")
def search_cmd(
    query: tuple[str, ...],
    top: int,
    json_output: bool,
    answer: bool,
    content: bool,
    language: tuple[str, ...],
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    agentic: bool,
    max_subqueries: int,
    semantic_only: bool,
    rerank: bool,
    rerank_pool: int,
    rerank_model: str,
    hyde: bool,
    multi_resolution: bool,
    file_top: int,
    lexical_prefilter: bool,
    lexical_root: str,
    lexical_min_candidates: int,
    agent_mode: str,
    agent_fast: bool,
    agent_context: bool,
    agent_daemon: bool,
    daemon_url: str,
    rank_by: str,
    cascade: bool,
    cascade_tau: float,
    auto_index: bool | None,
    rg_shortcut: bool,
    filename_shortcut: bool,
    llm_router: bool,
    detail: str,
    ocr: bool,
    lazy: bool,
    explain: bool,
):
    """Run a search. Aliased as the bare form: ``skygrep "<query>"``.

    Information depth:

      \b
      skygrep "where is the project brief?"
          Locate files/folders quickly.

      \b
      skygrep --content --detail standard "what does the migration plan say?"
          Show source/document snippets.

      \b
      skygrep --content --detail full --include "docs/migration-plan.md" "show steps"
          Read deeper after narrowing to one file or folder.

      \b
      skygrep --detail "show steps"
          Shorthand for --detail full "show steps".

      \b
      skygrep --agent-fast "where is token refresh?"
          Fast path discovery for LLM agents.

      \b
      skygrep --agent-context --include "src/**" "what does token refresh do?"
          Machine-readable context for LLM agents.

      \b
      skygrep --agent-daemon --agent-context "what changed in the cache layer?"
          Reuse a running `skygrep serve` process for repeated agent calls.
    """
    query = _normalize_query_args(query)
    _auto_refresh_setup_snippets()
    command_start = time.perf_counter()

    def _wall_elapsed() -> float:
        return time.perf_counter() - command_start

    # Intelligent CLI hint — out-of-scope query detection.
    #
    # 0.2.4 originally fired this *before* the LLM router decision was
    # available (using a pure keyword list). 0.2.6 moved the primary
    # detector to the LLM router prompt itself, so we now defer this
    # render to AFTER ``decision`` is computed and pass it through.
    # The keyword list survives as the offline fallback inside
    # ``detect_out_of_scope`` for when the LLM is unreachable.
    #
    # Note: still gated by ``hints_disabled()`` and the search still
    # runs after the hint so the user is never blocked.

    import os as _os

    selected_agent_modes = [
        mode
        for mode, enabled in (
            ("agent-mode", agent_mode != "off"),
            ("agent-fast", agent_fast),
            ("agent-context", agent_context),
        )
        if enabled
    ]
    if len(selected_agent_modes) > 1:
        raise click.UsageError(
            "Choose only one agent preset: --agent-mode, --agent-fast, or --agent-context."
        )
    if agent_fast:
        agent_mode = "fast"
    elif agent_context:
        agent_mode = "context"
    if agent_mode != "off":
        json_output = True
        rerank = False
        if agent_mode == "fast":
            content = False
            detail = "brief"
            if top == 5:
                top = 10
        elif agent_mode == "context":
            content = True
            detail = "standard"
            if top == 5:
                top = 8
        elif agent_mode == "deep":
            content = True
            detail = "full"
        elif agent_mode == "answer":
            answer = True
            content = True
            # Synthesized answers are intentionally human-readable; keep
            # JSON off unless the caller explicitly requested it.
            json_output = False
        if agent_daemon and not daemon_url and agent_mode != "answer":
            daemon_url = _os.environ.get("SKYGREP_DAEMON_URL", _DEFAULT_AGENT_DAEMON_URL)
    elif agent_daemon and not daemon_url:
        daemon_url = _os.environ.get("SKYGREP_DAEMON_URL", _DEFAULT_AGENT_DAEMON_URL)
    # Answer synthesis needs enough independent evidence to enumerate
    # checklists and multi-part procedures completely. Keep explicit user
    # limits authoritative while making the default answer context as rich as
    # the agent-context preset.
    if answer and top == 5 and not _click_option_explicit("top"):
        top = 8
    machine_context = (agent_mode in {"fast", "context", "deep"}) or (
        json_output and not answer
    )
    llm_router = _effective_llm_router_for_agent_mode(
        agent_mode,
        llm_router,
        llm_router_explicit=_click_option_explicit("llm_router"),
    )
    cascade = _effective_cascade_for_agent_mode(
        agent_mode,
        cascade,
        cascade_explicit=_click_option_explicit("cascade"),
    )
    router_timeout_s = (
        _env_float("SKYGREP_AGENT_ROUTER_TIMEOUT_S", 1.5, minimum=0.2)
        if machine_context
        else None
    )
    model_call_timeout_s = _env_float(
        "SKYGREP_AGENT_MODEL_TIMEOUT_S" if machine_context else "SKYGREP_FOREGROUND_MODEL_TIMEOUT_S",
        3.0 if machine_context else 12.0,
        minimum=0.5,
    )
    cascade_timeout_s = _env_float(
        "SKYGREP_AGENT_CASCADE_TIMEOUT_S" if machine_context else "SKYGREP_CASCADE_TIMEOUT_S",
        8.0 if machine_context else 30.0,
        minimum=0.5,
    )

    config = get_config()
    if auto_index is None:
        # Default policy: auto-index unless the caller has pinned the DB
        # location with SKYGREP_DB_PATH (curated index — don't auto-mutate it).
        auto_index = _os.environ.get("SKYGREP_DB_PATH") is None
    project_root = cfg_mod.project_root()
    scope_facet = resolve_scope_facet(query, project_root)
    explicit_scope = scope_facet is not None
    if scope_facet is not None:
        project_root = scope_facet.root
        if _os.environ.get("SKYGREP_DB_PATH") is None:
            config["db_path"] = cfg_mod.project_db_path(project_root)
        if not json_output:
            click.echo(
                _ui_step(
                    "scope",
                    f"{project_root} · {scope_facet.reason} "
                    f"(conf={scope_facet.confidence:.2f})",
                ),
                err=True,
            )
    if daemon_url:
        from .server import daemon_search

        start = time.time()
        try:
            payload = daemon_search(
                daemon_url,
                query,
                top_k=top,
                rerank=rerank,
                rerank_pool=rerank_pool if rerank_pool is not None else config["rerank_pool"],
                multi_resolution=multi_resolution,
                file_top=file_top,
                hyde=hyde,
                languages=tuple(language),
                include_patterns=tuple(include_patterns),
                exclude_patterns=tuple(exclude_patterns),
            )
        except Exception as exc:
            click.echo(f"daemon error: {exc}; falling back to in-process search", err=True)
        else:
            elapsed = time.time() - start
            results = payload.get("results", [])
            results = _apply_result_boundaries(
                results,
                project_root=project_root,
                explicit_scope=explicit_scope,
                include_patterns=tuple(include_patterns),
                exclude_patterns=tuple(exclude_patterns),
            )
            if json_output:
                click.echo(render_json_results(results, include_snippet=content))
                return
            if explain:
                _attach_explain(results, None)
            for r in results:
                click.echo(render_terminal_result(
                    r, content=content, detail=detail, ocr=ocr,
                    explain=explain,
                ))
            click.echo(
                f"\n[Daemon search completed in {_wall_elapsed():.3f}s; "
                f"daemon-side {payload.get('latency_seconds')}s]"
            )
            return

    db_path = config["db_path"]

    # Metadata queries ("latest files I opened", "recently modified
    # files") are not content search. Answer them from filesystem
    # timestamps before any Ollama preheat, router call, index check, or
    # lazy semantic path can add seconds of irrelevant work.
    metadata_start = time.time()
    metadata_hits, metadata_query = metadata_results(query, project_root, top_k=top)
    metadata_hits = _apply_result_boundaries(
        metadata_hits,
        project_root=project_root,
        explicit_scope=explicit_scope,
        include_patterns=tuple(include_patterns),
        exclude_patterns=tuple(exclude_patterns),
    )
    metadata_filters_active = bool(include_patterns or exclude_patterns)
    if metadata_query is not None and (metadata_hits or not metadata_filters_active):
        elapsed = time.time() - metadata_start
        if json_output:
            click.echo(render_json_results(metadata_hits, include_snippet=content))
            return
        if metadata_hits:
            click.echo(
                _ui_step("metadata", f"{metadata_query.kind} file matches:"),
                err=True,
            )
            for r in metadata_hits:
                click.echo(
                    render_terminal_result(
                        r,
                        content=content,
                        project_root=str(project_root),
                        detail=detail,
                        ocr=ocr,
                        explain=explain,
                    )
                )
            click.echo(_ui_done(_wall_elapsed(), "BEST"))
            click.echo(_ui_rows([
                ("path", f"metadata-{metadata_query.kind}"),
                ("reason", metadata_query.reason),
                ("pool", f"{len(metadata_hits)} files · semantic-skipped"),
            ]))
        else:
            click.echo(
                "No matching files found for that metadata query in the "
                "current search scope.",
                err=True,
            )
            click.echo(_ui_done(_wall_elapsed(), "EMPTY"))
            click.echo(_ui_rows([
                ("path", f"metadata-{metadata_query.kind}"),
                ("reason", metadata_query.reason),
            ]))
        return

    # Fire-and-forget Ollama preheat. Loads embed + HyDE models with
    # ``keep_alive=-1`` in background threads so the cold-load cost
    # (~5-10 s per model on Mac CPU) is amortised across the time we
    # spend on rg prefilter, file-mean cosine, and DB migrations.
    # Best-effort: failures are silently swallowed inside ``preheat_models``.
    bootstrap.preheat_models()

    from . import auto_index as ai

    # v0.15.0 LLM-driven query routing. Resolves query intent + token
    # selection + skip decisions via local Ollama. Falls back to the
    # v0.14.0 rule-based classifier on any failure. The decision is
    # consulted by the filename / lexical / cascade tier dispatchers
    # below.
    # Persistent global router-decision cache. The router's decision
    # depends on query phrasing only, not on project content, so a
    # single SQLite shared across projects is sound — same query
    # never pays the LLM cost twice within or across sessions. Drop
    # ~/.skylakegrep/router_cache.db to force re-classification.
    # 0.5.8: if Ollama isn't running but is installed, autostart it in the
    # background so the LLM router (and embedder for non-cached queries)
    # don't silently fall back to rule-based mode. This is best-effort:
    # the function returns False fast on a missing binary or 5 s timeout,
    # and downstream code already has a clean rule-based fallback.
    if llm_router:
        try:
            bootstrap.try_autostart_ollama()
        except Exception as exc:  # noqa: BLE001 — never block search on startup
            logger.debug("ollama autostart skipped: %s", exc)

    router_start = time.time()
    _router_cache_db = None
    try:
        from pathlib import Path as _P
        _cache_dir = _P.home() / ".skylakegrep"
        _cache_dir.mkdir(parents=True, exist_ok=True)
        _router_cache_db = sqlite3.connect(str(_cache_dir / "router_cache.db"))
    except (OSError, sqlite3.Error):
        _router_cache_db = None
    decision: RouterDecision = route_query(
        query,
        conn=_router_cache_db,
        use_llm=llm_router,
        timeout=router_timeout_s,
    )
    if _router_cache_db is not None:
        try:
            _router_cache_db.close()
        except sqlite3.Error:
            pass
    router_elapsed = time.time() - router_start
    _suppress_nonterminal_out_of_scope_for_scope(
        query, decision, explicit_scope=explicit_scope
    )

    # 0.5.8+: surface the routing path early in human output so users and
    # agent wrappers can see whether this invocation is path-depth,
    # semantic-depth, metadata, or mixed before deeper tiers start.
    # Printed to stderr so it never pollutes JSON / piped output.
    if not json_output:
        _hdr = _format_router_explain(decision, include_reason=explain)
        if _hdr:
            click.echo(_hdr, err=True)

    # Intelligent CLI hint — out-of-scope query detection (0.2.6+).
    # Now driven by the LLM router's ``out_of_scope`` field
    # (``content`` / ``recency`` / ``size`` / ``listing``) rather than
    # the 0.2.4–0.2.5 keyword list. The keyword list survives only as
    # an offline fallback inside ``detect_out_of_scope`` when the LLM
    # is unreachable. See ``docs/PRINCIPLES.md`` Principle 1
    # ("Understanding > Enumeration") for the rationale.
    if not hints_disabled():
        _oos_hint = detect_out_of_scope(query, decision=decision)
        if _oos_hint is not None:
            click.echo(render_out_of_scope_hint(_oos_hint, query), err=True)

    # v0.14.0 hierarchical merge: collect filename-lookup results
    # without short-circuiting. The merged results from all enabled
    # tiers are ranked by `classify_intent(query)` later. This runs
    # BEFORE the index-ready check so the filename tier works on
    # un-indexed directories (~/Downloads etc.).
    fn_results: list[dict] = []
    fn_elapsed = 0.0
    filename_shortcut_allowed = (
        filename_shortcut and not decision.skip_filename and not agentic
    )
    if (
        agent_mode in {"context", "deep"}
        and content
        and not _click_option_explicit("filename_shortcut")
    ):
        filename_shortcut_allowed = False
    if filename_shortcut_allowed:
        fn_start = time.time()
        fn_hits = ai.filename_shortcut(
            query, project_root, top_k=top, decision=decision
        )
        fn_elapsed = time.time() - fn_start
        if fn_hits:
            fn_results = _apply_result_boundaries(
                fn_hits,
                project_root=project_root,
                explicit_scope=explicit_scope,
                include_patterns=tuple(include_patterns),
                exclude_patterns=tuple(exclude_patterns),
            )
            if explain:
                _attach_explain(fn_results, decision)
    filename_answered = (
        bool(fn_results)
        and _filename_evidence_satisfies_depth(
            query,
            decision,
            detail=detail,
            answer=answer,
            agentic=agentic,
        )
    )
    if filename_answered:
        elapsed = router_elapsed + fn_elapsed
        index_note = "refresh skipped (filename fast path)"
        if auto_index:
            fast_conn = None
            try:
                fast_conn = init_db(db_path)
                if not ai.is_index_ready(fast_conn):
                    spawned = ai.spawn_background_index(project_root, db_path)
                    index_note = (
                        "background indexing queued"
                        if spawned is not None
                        else "background indexing already running"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("filename fast-path background index check failed: %s", exc)
            finally:
                if fast_conn is not None:
                    try:
                        fast_conn.close()
                    except sqlite3.Error:
                        pass
        if json_output:
            click.echo(render_json_results(fn_results, include_snippet=content))
            return
        click.echo(_ui_step("filename", "matches:"), err=True)
        for r in fn_results:
            click.echo(
                render_terminal_result(
                    r,
                    content=content,
                    project_root=str(project_root),
                    detail=_render_detail_for_result(r, detail, decision),
                    ocr=ocr,
                    explain=explain,
                )
            )
        click.echo(_ui_done(_wall_elapsed(), "BEST"))
        click.echo(_ui_rows([
            ("path", "filename-lookup"),
            (
                "router",
                f"{decision.source} -> intent={decision.intent} "
                f"({decision.confidence:.2f})",
            ),
            (
                "pool",
                f"{len(fn_results)} filename + 0 lexical · cascade-skipped",
            ),
            ("index", index_note),
        ]))
        return

    # Scoped artifact-location + metadata modifier lane.
    #
    # Example shape: "where is the report I recently created in PROJECT
    # folder". Scope, target descriptors, and metadata are already separate
    # query-plan facets; this lane uses that structure directly instead of
    # falling through to semantic embedding. It is final only for path-depth
    # mixed/filename asks. Semantic / answer / agentic calls can still use
    # the same anchors later while deeper retrieval continues.
    descriptor_results: list[dict] = []
    descriptor_elapsed = 0.0
    if (
        explicit_scope
        and not getattr(decision, "metadata_terminal", False)
        and getattr(decision, "metadata_kind", None)
        and not decision.skip_filename
    ):
        desc_start = time.time()
        descriptor_results, _descriptor_facet = descriptor_file_results(
            query,
            project_root,
            top_k=top,
        )
        descriptor_results = _apply_result_boundaries(
            descriptor_results,
            project_root=project_root,
            explicit_scope=explicit_scope,
            include_patterns=tuple(include_patterns),
            exclude_patterns=tuple(exclude_patterns),
        )
        descriptor_elapsed = time.time() - desc_start
        if descriptor_results and explain:
            _attach_explain(descriptor_results, decision)

    descriptor_answered = _descriptor_file_evidence_satisfies_depth(
        descriptor_results,
        decision,
        answer=answer,
        agentic=agentic,
    )
    if descriptor_answered:
        elapsed = router_elapsed + fn_elapsed + descriptor_elapsed
        index_note = "refresh skipped (scoped file-discovery fast path)"
        if auto_index:
            fast_conn = None
            try:
                fast_conn = init_db(db_path)
                if not ai.is_index_ready(fast_conn):
                    spawned = ai.spawn_background_index(project_root, db_path)
                    index_note = (
                        "background indexing queued"
                        if spawned is not None
                        else "background indexing already running"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("scoped descriptor background index check failed: %s", exc)
            finally:
                if fast_conn is not None:
                    try:
                        fast_conn.close()
                    except sqlite3.Error:
                        pass
        if json_output:
            click.echo(render_json_results(descriptor_results, include_snippet=content))
            return
        click.echo(_ui_step("scope", "file metadata matches:"), err=True)
        for r in descriptor_results:
            click.echo(
                render_terminal_result(
                    r,
                    content=content,
                    project_root=str(project_root),
                    detail=detail,
                    ocr=ocr,
                    explain=explain,
                )
            )
        click.echo(_ui_done(_wall_elapsed(), "BEST"))
        click.echo(_ui_rows([
            ("path", "scoped-file-discovery"),
            (
                "router",
                f"{decision.source} -> intent={decision.intent} "
                f"({decision.confidence:.2f})",
            ),
            (
                "pool",
                f"{len(fn_results)} filename + "
                f"{len(descriptor_results)} scoped metadata · semantic-skipped",
            ),
            ("index", index_note),
        ]))
        return

    # Routing decision: ready → cascade; building or absent → rg fallback.
    # 0.5.1: when the cold-start path is taken (no index yet) and rg
    # alone is weak, that fallback now also fires the lazy LLM-routed
    # semantic tier and merges the two — wired below in the cold-start
    # branch. This stays AUTO so the user never has to know which tier
    # to ask for; the `--lazy / --no-lazy` flag only opts out.
    conn = init_db(db_path)
    answerer = None

    # Intelligent CLI hint — first-run nudge (0.2.4+). Once-per-project
    # greeting that explains the auto-index + rg-fallback flow so a
    # first-time user doesn't think the tool is broken or slow. The
    # ``mark_first_run_nudge_shown`` call records the metadata flag so
    # subsequent queries don't repeat the greeting.
    #
    # Gated on ``not json_output`` because JSON consumers (test runners,
    # downstream tooling, agents) don't want a chatty stderr greeting
    # to leak into their captured output, and CliRunner by default
    # merges stderr into stdout for ``--json`` calls.
    if (
        not hints_disabled()
        and not json_output
        and should_show_first_run_nudge(conn)
    ):
        click.echo(render_first_run_nudge(), err=True)
        mark_first_run_nudge_shown(conn)
    ready = ai.is_index_ready(conn)
    if not ready and auto_index:
        # Spawn (or no-op if already running) a detached indexer; do NOT
        # block the user. The next query that lands after the spawn
        # finishes will get full semantic results.
        try:
            ai.spawn_background_index(project_root, db_path)
        except Exception as exc:
            logger.warning("background index spawn failed: %s", exc)

        cold_filename_path_depth = (
            decision.intent == "filename"
            and not fn_results
            and not explicit_scope
            and _filename_evidence_satisfies_depth(
                query,
                decision,
                detail=detail,
                answer=answer,
                agentic=agentic,
            )
        )

        # A path-depth filename query does not need a broad content grep
        # before the out-of-scope filename lane. When the current scope is
        # a large home tree, rg can take tens of seconds, hiding the fast
        # answer behind the wrong tier. Try the bounded proactive filename
        # search first; if it gives concrete paths, return immediately and
        # let the background indexer continue independently.
        pre_rg_proactive_results: list = []
        pre_rg_proactive_elapsed = 0.0
        pre_rg_proactive_ran = False
        if cold_filename_path_depth:
            pre_rg_proactive_ran = True
            _proactive_live = None
            if not json_output:
                click.echo(
                    _ui_step(
                        "proactive",
                        "no local filename hit yet - searching configured "
                        "roots before broad keyword scan",
                    ),
                    err=True,
                )
                if ui_mod.live_animation_enabled(sys.stderr):
                    _proactive_live = ui_mod.LiveHelix(
                        "proactive",
                        stream=sys.stderr,
                    ).start("searching configured roots")
            try:
                proactive_start = time.time()
                from . import proactive as _proactive_cold_pre_rg
                pre_rg_proactive_results, pre_rg_tele = (
                    _proactive_cold_pre_rg.run_enhancers_parallel(
                        query,
                        decision,
                        [],
                        top_k=top,
                        ctx=_proactive_cold_pre_rg.ProactiveContext(
                            conn=conn,
                            project_root=project_root,
                            explicit_scope=explicit_scope,
                        ),
                    )
                )
                pre_rg_proactive_elapsed = time.time() - proactive_start
                if _proactive_live is not None:
                    _proactive_live.stop()
                if (
                    not json_output
                    and pre_rg_tele.get("timed_out")
                    and not pre_rg_proactive_results
                ):
                    click.echo(
                        _ui_step(
                            "budget",
                            "proactive filename search still running past the "
                            "foreground budget; falling back to project keywords",
                        ),
                        err=True,
                    )
            except Exception:
                if _proactive_live is not None:
                    _proactive_live.stop()
                logger.exception(
                    "pre-rg cold-start proactive filename search failed; "
                    "falling through to rg/lazy"
                )
                pre_rg_proactive_results = []
                pre_rg_proactive_elapsed = 0.0
            if pre_rg_proactive_results:
                rendered = _proactive_cold_pre_rg.render_proactive_output(
                    pre_rg_proactive_results,
                    content=content,
                    project_root=str(project_root),
                    detail=detail,
                    ocr=ocr,
                    explain=explain,
                )
                elapsed = router_elapsed + fn_elapsed + pre_rg_proactive_elapsed
                if json_output:
                    payload = []
                    for pr in pre_rg_proactive_results:
                        payload.extend(pr.extra_hits)
                    payload = _apply_result_boundaries(
                        payload,
                        project_root=project_root,
                        explicit_scope=explicit_scope,
                        include_patterns=tuple(include_patterns),
                        exclude_patterns=tuple(exclude_patterns),
                    )
                    click.echo(render_json_results(payload, include_snippet=content))
                    return
                if rendered:
                    click.echo(rendered)
                click.echo(_ui_done(_wall_elapsed(), "BEST"))
                click.echo(_ui_rows([
                    ("path", "proactive-filename"),
                    (
                        "router",
                        f"{decision.source} -> intent={decision.intent} "
                        f"({decision.confidence:.2f})",
                    ),
                    (
                        "pool",
                        f"0 filename + 0 rg + {len(pre_rg_proactive_results)} "
                        "proactive · rg/lazy-skipped",
                    ),
                    ("index", "building in background"),
                ]))
                return

        # 0.5.4 streaming UX: tell the user immediately that something
        # is happening. Otherwise the cold-start path stays silent for
        # the duration of the rg call (typically 100 ms but seconds on
        # a large home dir or with cold disk cache), then silent again
        # for the 5–30 s lazy embed pass — the user sees a frozen
        # prompt with no signal that the system is working.
        if not json_output:
            click.echo(
                _ui_step("scan", "ripgrep cold-start · scanning project keywords"),
                err=True,
            )
            if pre_rg_proactive_ran:
                click.echo(
                    _ui_detail(
                        "proactive filename lane found no foreground answer; "
                        "continuing with rg/lazy depth"
                    ),
                    err=True,
                )

        # Fall back to a pure-rg result for this query, merged with any
        # filename-lookup hits collected above so the cold-start path
        # also benefits from the v0.14.0 hierarchical-merge model.
        start = time.time()
        lexical_query = simplify_router_query(strip_scope_clauses(query) or query)
        rg_cold = ai.rg_fallback_results(lexical_query, project_root, top_k=top)
        rg_elapsed = time.time() - start

        # Filename intent on a cold, unindexed project is the exact case
        # where semantic lazy search is most likely to waste time: the
        # answer may simply be outside cwd (Downloads/Desktop/Documents).
        # Run the bounded filename proactive tier before lazy embedding;
        # if it finds concrete basename evidence, return immediately.
        if (
            decision.intent == "filename"
            and not fn_results
            and not pre_rg_proactive_ran
            and not explicit_scope
            and _filename_evidence_satisfies_depth(
                query,
                decision,
                detail=detail,
                answer=answer,
                agentic=agentic,
            )
        ):
            try:
                proactive_start = time.time()
                from . import proactive as _proactive_cold_early
                early_proactive, _ = _proactive_cold_early.run_enhancers_parallel(
                    query,
                    decision,
                    [],
                    top_k=top,
                    ctx=_proactive_cold_early.ProactiveContext(
                        conn=conn,
                        project_root=project_root,
                        explicit_scope=explicit_scope,
                    ),
                )
                proactive_elapsed = time.time() - proactive_start
            except Exception:
                logger.exception(
                    "early cold-start proactive filename search failed; "
                    "falling through to lazy semantic"
                )
                early_proactive = []
                proactive_elapsed = 0.0
            if early_proactive:
                rendered = _proactive_cold_early.render_proactive_output(
                    early_proactive,
                    content=content,
                    project_root=str(project_root),
                    detail=detail,
                    ocr=ocr,
                    explain=explain,
                )
                elapsed = rg_elapsed + proactive_elapsed
                if json_output:
                    payload = []
                    for pr in early_proactive:
                        payload.extend(pr.extra_hits)
                    payload = _apply_result_boundaries(
                        payload,
                        project_root=project_root,
                        explicit_scope=explicit_scope,
                        include_patterns=tuple(include_patterns),
                        exclude_patterns=tuple(exclude_patterns),
                    )
                    click.echo(render_json_results(payload, include_snippet=content))
                    return
                if rendered:
                    click.echo(rendered)
                click.echo(_ui_done(_wall_elapsed(), "BEST"))
                click.echo(_ui_rows([
                    ("path", "proactive-filename"),
                    (
                        "router",
                        f"{decision.source} -> intent={decision.intent} "
                        f"({decision.confidence:.2f})",
                    ),
                    (
                        "pool",
                        f"0 filename + {len(rg_cold)} rg + "
                        f"{len(early_proactive)} proactive · lazy-skipped",
                    ),
                    ("index", "building in background"),
                ]))
                return

        # 0.5.1 auto-trigger lazy semantic on a weak rg cold-start.
        # The user can't be expected to know whether they're in the
        # right folder or whether their keyword query happens to align
        # with the code's vocabulary — so we decide for them: if rg
        # already returns a strong keyword answer (≥ 3 results AND at
        # least one path contains a query term ≥ 3 chars), the user
        # gets that instantly and we skip the 5 s lazy embed pass. If
        # rg is weak (no path-token overlap or < 3 hits), we fire the
        # LLM-routed lazy semantic tier in the same call and merge.
        # `--no-lazy` opts out (e.g. for benchmarking pure rg).
        def _rg_is_strong(results_: list, query_: str) -> bool:
            if not results_ or len(results_) < 3:
                return False
            import re as _re
            # Tokenize query terms with camelCase / PascalCase splitting
            # so "ModelForm" → {"modelform", "model", "form"} and matches
            # both `ModelForm.py` and `model_form.py` paths.
            raw = [t for t in query_.split() if len(t) >= 3]
            terms: set[str] = set()
            for t in raw:
                terms.add(t.lower())
                # Pascal/camelCase split: "ModelForm" → ["Model", "Form"]
                for p in _re.findall(r"[A-Z][a-z]+|[a-z]{3,}|\d+", t):
                    if len(p) >= 3:
                        terms.add(p.lower())
            if not terms:
                return False
            matched: set[str] = set()
            for r in results_:
                raw_path = str(r.get("path", "")).lower()
                # Normalize path by stripping separators so
                # `model_form` and `model-form` reduce to `modelform`.
                norm_path = raw_path.replace("_", "").replace("-", "")
                for t in terms:
                    if t in raw_path or t in norm_path:
                        matched.add(t)
            # rg is "strong" only when ≥ 2 distinct query tokens land
            # in paths. A single common token like `schema` matching
            # 5 unrelated `schema.py` files in different backends is
            # NOT strong — that's how vocabulary-mismatch queries
            # ("schema synchronization across releases") fool a naive
            # gate. Single-word queries with no camel/snake case split
            # always fire lazy, by design — the 5 s wait is cheaper
            # than the wrong answer.
            return len(matched) >= 2

        lazy_results: list = []
        lazy_tele: dict = {}
        cross_results: list = []
        cross_tele: dict = {}
        lazy_elapsed = 0.0
        rg_strong = _rg_is_strong(rg_cold, lexical_query)
        cold_lexical_answered = _lexical_evidence_satisfies_depth(
            query,
            rg_cold,
            decision,
            detail=detail,
            answer=answer,
            agentic=agentic,
        )
        if cold_lexical_answered:
            rg_strong = True

        # 0.5.4 streaming UX — STAGE 1: when lazy is about to fire (rg
        # is weak), print the rg + filename preliminary hits *first*
        # so the user has something to look at while we spend 5–30 s
        # on the embed pass. The lazy-augmentation results are then
        # printed after the lazy call returns. Tracked in
        # ``early_printed_paths`` so STAGE 2 only echoes the newly-
        # found-by-lazy results.
        early_printed_paths: set = set()
        early_printed_results: dict[str, dict] = {}
        will_fire_lazy = lazy and not rg_strong
        if will_fire_lazy and not json_output and not answer:
            preliminary: list = []
            seen_p: set = set()
            for source in (fn_results, rg_cold):
                for r in source:
                    p = r.get("path", "")
                    if not p or p in seen_p:
                        continue
                    seen_p.add(p)
                    preliminary.append(r)
                    if len(preliminary) >= top:
                        break
                if len(preliminary) >= top:
                    break
            if preliminary:
                prelim_label = (
                    "preliminary filename anchors + keyword matches"
                    if fn_results
                    else "preliminary keyword matches"
                )
                click.echo(
                    _ui_step(
                        "seed",
                        f"{prelim_label} (lazy semantic refinement starting):",
                    ),
                    err=True,
                )
                for r in preliminary:
                    click.echo(
                        render_terminal_result(
                            r,
                            content=content,
                            project_root=str(project_root),
                            detail=_render_detail_for_result(r, detail, decision),
                            ocr=ocr,
                            explain=explain,
                        )
                    )
                    printed_path = r.get("path", "")
                    early_printed_paths.add(printed_path)
                    if printed_path:
                        early_printed_results[printed_path] = r
                click.echo("", err=True)
            else:
                click.echo(
                    _ui_step(
                        "lazy",
                        "no keyword matches yet - lazy semantic search "
                        "exploring within the foreground budget",
                    ),
                    err=True,
                )
        # 0.5.3 cold-start lazy + cross-folder dispatch.
        #
        #   rg has hits, paths overlap query tokens   → rg-only, skip lazy
        #   rg has hits, paths weak (vocab-mismatch)  → lazy_cwd
        #   rg has zero hits (probably wrong folder)  → lazy_cwd ∥ lazy_cross_folder
        #
        # The third case is the user's "wrong path" scenario:
        # rg returned nothing in cwd, so the answer may be in a sibling
        # folder. Both lazy paths run in parallel. If the filename tier
        # already found a local anchor, this is no longer wrong-folder:
        # keep refinement focused on cwd so the same invocation upgrades
        # the anchor to content instead of diffusing across unrelated
        # home roots.
        cold_wrong_folder = (
            lazy
            and len(rg_cold) == 0
            and not fn_results
            and not explicit_scope
        )
        if lazy and not rg_strong:
            from concurrent.futures import ThreadPoolExecutor as _TPE
            from concurrent.futures import TimeoutError as _FuturesTimeout
            from . import lazy_indexer as LZ

            # Wire a stderr progress sink unless --json is requested.
            _live_status = None
            if not json_output and ui_mod.live_animation_enabled(sys.stderr):
                _live_status = ui_mod.LiveHelix("semantic", stream=sys.stderr)
                _live_status.start("starting foreground search")
                _progress = _live_status.update
            else:
                _progress = None if json_output else LZ._stderr_progress

            def _stop_live_status() -> None:
                nonlocal _live_status
                if _live_status is not None:
                    _live_status.stop()
                    _live_status = None

            def _env_int_local(name: str, default: int) -> int:
                try:
                    return max(1, int(_os.environ.get(name, str(default))))
                except ValueError:
                    return default

            def _env_float_local(name: str, default: float) -> float:
                try:
                    return max(1.0, float(_os.environ.get(name, str(default))))
                except ValueError:
                    return default

            lazy_seed_budget = _env_int_local(
                "SKYGREP_COLD_LAZY_SEED_BUDGET", 12
            )
            cross_seed_budget = _env_int_local(
                "SKYGREP_COLD_CROSS_SEED_BUDGET", 3
            )
            total_lazy_budget_s = _env_float_local(
                "SKYGREP_COLD_LAZY_TOTAL_BUDGET_S", 8.0
            )
            cwd_lazy_budget_s = _env_float_local(
                "SKYGREP_COLD_LAZY_CWD_BUDGET_S", 5.0
            )
            cross_lazy_budget_s = _env_float_local(
                "SKYGREP_COLD_LAZY_CROSS_BUDGET_S", 2.5
            )
            cwd_embed_timeout_s = _env_float_local(
                "SKYGREP_COLD_LAZY_EMBED_TIMEOUT_S", 4.0
            )
            cross_embed_timeout_s = _env_float_local(
                "SKYGREP_COLD_CROSS_EMBED_TIMEOUT_S", 2.0
            )
            lazy_router_timeout_s = _env_float_local(
                "SKYGREP_COLD_LAZY_ROUTER_TIMEOUT_S", 1.0
            )

            def _apply_foreground_embed_timeout(embedder, seconds: float) -> None:
                try:
                    setattr(embedder, "request_timeout_s", seconds)
                    setattr(embedder, "batch_timeout_s", seconds)
                    setattr(embedder, "allow_per_chunk_fallback", False)
                except Exception:
                    pass

            def _is_db_locked(exc: Exception) -> bool:
                return (
                    isinstance(exc, sqlite3.OperationalError)
                    and "locked" in str(exc).lower()
                )

            # 0.5.7: each parallel worker opens its OWN SQLite
            # connection — sqlite3 forbids cross-thread reuse of a
            # connection. Without this, the cold+wrong-folder
            # branch printed "lazy cross-folder failed: SQLite
            # objects created in a thread can only be used in that
            # same thread" and silently returned empty results.
            # The proactive umbrella's filename_extend tier still
            # delivered an answer in 1 s, but the lazy semantic
            # tier was a dead path. Same fix pattern as the
            # cascade worker thread in 0.5.6.
            def _run_cwd():
                try:
                    _wconn = init_db(db_path)
                    embedder_cwd = get_embedder(role="query")
                    _apply_foreground_embed_timeout(
                        embedder_cwd, cwd_embed_timeout_s
                    )
                    try:
                        return LZ.lazy_explore_cold_start(
                            _wconn, lexical_query, project_root, embedder_cwd,
                            top_k=top,
                            seed_budget=lazy_seed_budget,
                            total_budget_s=min(
                                cwd_lazy_budget_s, total_lazy_budget_s
                            ),
                            router_timeout_s=lazy_router_timeout_s,
                            progress=_progress,
                        )
                    finally:
                        try:
                            _wconn.close()
                        except Exception:
                            pass
                except Exception as exc:  # noqa: BLE001
                    if _is_db_locked(exc):
                        return [], {
                            "path": "lazy-cold-start",
                            "db_locked": True,
                        }
                    logger.debug("lazy cold-start cwd failed: %s", exc)
                    return [], {}

            def _run_cross():
                try:
                    _wconn = init_db(db_path)
                    embedder_cross = get_embedder(role="query")
                    _apply_foreground_embed_timeout(
                        embedder_cross, cross_embed_timeout_s
                    )
                    try:
                        return LZ.lazy_explore_cross_folder(
                            _wconn, lexical_query, embedder=embedder_cross,
                            top_k=top,
                            seed_budget=cross_seed_budget,
                            progress=_progress,
                        )
                    finally:
                        try:
                            _wconn.close()
                        except Exception:
                            pass
                except Exception as exc:  # noqa: BLE001
                    if _is_db_locked(exc):
                        return [], {
                            "path": "lazy-cross-folder",
                            "db_locked": True,
                        }
                    logger.debug("lazy cross-folder failed: %s", exc)
                    return [], {}

            lazy_start = time.time()
            if cold_wrong_folder:
                _pool = _TPE(max_workers=2)
                f_cwd = _pool.submit(_run_cwd)
                f_cross = _pool.submit(_run_cross)
                deadline = time.time() + total_lazy_budget_s
                try:
                    lazy_results, lazy_tele = f_cwd.result(
                        timeout=min(cwd_lazy_budget_s, total_lazy_budget_s)
                    )
                except _FuturesTimeout:
                    lazy_results, lazy_tele = [], {
                        "path": "lazy-cold-start",
                        "timed_out": True,
                    }
                    if not json_output:
                        _stop_live_status()
                        click.echo(
                            _ui_step(
                            "budget",
                            "cwd lazy semantic search hit the foreground "
                            "budget; continuing with any sibling-folder evidence",
                        ),
                            err=True,
                        )
                remaining = max(0.0, deadline - time.time())
                try:
                    cross_results, cross_tele = f_cross.result(
                        timeout=min(cross_lazy_budget_s, remaining)
                    )
                except _FuturesTimeout:
                    cross_results, cross_tele = [], {
                        "path": "lazy-cross-folder",
                        "timed_out": True,
                    }
                    if not json_output:
                        _stop_live_status()
                        click.echo(
                            _ui_step(
                            "budget",
                            "cross-folder lazy search hit the foreground "
                            "budget; background indexing will continue.",
                            ),
                            err=True,
                        )
                finally:
                    _pool.shutdown(wait=False, cancel_futures=True)
            else:
                lazy_results, lazy_tele = _run_cwd()
                if lazy_tele.get("db_locked") and not json_output:
                    _stop_live_status()
                    click.echo(
                        _ui_step(
                            "busy",
                            "cwd lazy semantic search skipped because the "
                            "background index is writing; retry shortly or run "
                            "`skygrep stats`.",
                        ),
                        err=True,
                    )
            _stop_live_status()
            lazy_elapsed = time.time() - lazy_start
            if cross_tele.get("db_locked") and not json_output:
                _stop_live_status()
                click.echo(
                    _ui_step(
                        "busy",
                        "cross-folder lazy search skipped because the "
                        "background index is writing; retry shortly or run "
                        "`skygrep stats`.",
                    ),
                    err=True,
                )

        elapsed = rg_elapsed + lazy_elapsed
        intent = decision.intent
        # 0.5.3 cold-start merge: when lazy fired (only happens when
        # the rg-quality gate already classified rg as weak), trust
        # lazy's σ-validated cosine ranking for the primary top-K
        # slots and only use rg for de-duplicated backfill. Without
        # this the prior `merge_tiers(lexical=rg, semantic=lazy)`
        # call applied intent-aware ranking that reliably buried the
        # lazy answer beneath rg's score-1.0 hits — visible as 1/10
        # auto-trigger hit rate on the Django oracle bench. With
        # lazy-priority, the lazy module's top-K reaches the user
        # whenever it fired. Filename hits stay as anchors, but a later
        # semantic result for the same path upgrades the anchor to
        # content so semantic-depth queries do not end as metadata-only
        # filename cards.
        if lazy_results or cross_results:
            # filename → cwd-lazy → cross-folder → rg backfill,
            # deduped by path. Cross-folder is appended AFTER cwd-lazy
            # because when cwd genuinely contains the answer (rg
            # returned 0 because of vocabulary mismatch, but the file
            # IS local), we want the cwd hit to win.
            results = _merge_sources_preferring_depth(
                (fn_results, lazy_results, cross_results, rg_cold),
                top=top,
            )
        elif _semantic_filename_anchor_should_lead(decision, fn_results):
            results = _merge_sources_preferring_depth(
                (fn_results, rg_cold),
                top=top,
            )
        else:
            results = merge_tiers(
                filename=fn_results,
                lexical=rg_cold,
                semantic=[],
                intent=intent,
                top_k=top,
            )
        results = _apply_adaptive_metadata_ranking(results, decision)
        results = _apply_result_boundaries(
            results,
            project_root=project_root,
            explicit_scope=explicit_scope,
            include_patterns=tuple(include_patterns),
            exclude_patterns=tuple(exclude_patterns),
        )
        if explain:
            _attach_explain(results, decision)
        if json_output:
            _augment_filename_content_for_machine(
                results, query, decision, detail=detail, ocr=ocr,
            )
            if machine_context:
                evidence_floor = _env_float(
                    "SKYGREP_AGENT_MIN_EVIDENCE_SCORE", 0.50, minimum=0.0
                )
                results = _filter_low_evidence_machine_results(
                    results, query, min_score=evidence_floor
                )
            click.echo(render_json_results(results, include_snippet=content))
            return
        # 0.2.8: try proactive enhancers in the cold-start path too.
        # 0.2.7 only ran proactive on the main cascade path, which
        # meant the user-reported case (filename lookup against an
        # un-indexed dir, rg can't grep the binary target) hit the
        # ``return`` below before proactive ever got a chance to
        # search ~/Downloads / ~/Desktop / ~/Documents. Let the
        # framework run here so cold-start users get the same
        # proactive helpfulness as warm queries.
        # 0.2.11: pass ctx with conn so enhancers like
        # ``recovery_progress_hint`` can read live recovery state.
        cold_proactive_results: list = []
        try:
            from . import proactive as _proactive
            cold_proactive_results, _ = _proactive.run_enhancers_parallel(
                query, decision, results, top_k=top,
                ctx=_proactive.ProactiveContext(
                    conn=conn,
                    project_root=project_root,
                    explicit_scope=explicit_scope,
                ),
            )
        except Exception:
            logger.exception(
                "proactive enhancer in cold-start path failed; ignoring"
            )
        if not results and not cold_proactive_results:
            click.echo(
                "No matches yet. Semantic index is building in the background; "
                "try the same query again in a minute, or run `skygrep stats` to "
                "see progress.",
                err=True,
            )
            return
        if answer:
            answer_results = list(results)
            for pr in cold_proactive_results:
                answer_results.extend(pr.extra_hits)
            _augment_filename_content_for_machine(
                answer_results, query, decision, detail=detail, ocr=ocr,
                for_answer=True,
            )
            if answerer is None:
                answerer = get_answerer()
            synthesized = answerer.answer(query, answer_results)
            click.echo(synthesized)
            click.echo("\nSources:")
            for result in answer_results:
                click.echo(render_compact_source(result))
            click.echo(f"\n[Answer completed in {_wall_elapsed():.3f}s]")
            return
        # 0.5.4 streaming UX — STAGE 2: if STAGE 1 already printed
        # preliminary keyword hits, only echo NEW results found by the
        # lazy semantic pass (deduped against the early printed set).
        # Otherwise (rg-strong fast path or json mode), print the full
        # ranked top-K as before.
        if early_printed_paths:
            new_results = [
                r for r in results
                if r.get("path", "") and (
                    r.get("path", "") not in early_printed_paths
                    or _result_is_depth_upgrade(
                        early_printed_results.get(r.get("path", "")),
                        r,
                    )
                )
            ]
            if new_results:
                click.echo(
                    _ui_step("refine", "matches from lazy semantic search:"),
                    err=True,
                )
                for r in new_results:
                    click.echo(
                        render_terminal_result(
                            r,
                            content=content,
                            project_root=str(project_root),
                            detail=_render_detail_for_result(r, detail, decision),
                            ocr=ocr,
                            explain=explain,
                        )
                    )
            else:
                # Lazy didn't find anything beyond what rg already
                # surfaced. Tell the user explicitly so they don't
                # think the system hung.
                click.echo(
                    _ui_step(
                        "refine",
                        "lazy semantic search added no new matches "
                        "(top-K above is the final answer).",
                    ),
                    err=True,
                )
        else:
            for r in results:
                click.echo(
                    render_terminal_result(
                        r,
                        content=content,
                        project_root=str(project_root),
                        detail=_render_detail_for_result(r, detail, decision),
                        ocr=ocr,
                        explain=explain,
                    )
                )
        if cold_proactive_results:
            rendered = _proactive.render_proactive_output(
                cold_proactive_results,
                content=content,
                project_root=str(project_root),
                detail=detail,
                ocr=ocr,
                explain=explain,
            )
            if rendered:
                click.echo(rendered)
        building = ai.is_index_building(db_path)
        suffix = "building in background" if building else "queued"
        proactive_tag = (
            f" + {len(cold_proactive_results)} proactive"
            if cold_proactive_results else ""
        )
        if lazy_results or cross_results or lazy_tele or cross_tele:
            lazy_tag = (
                f" + lazy auto (σ={lazy_tele.get('sigma', 0):.3f}, "
                f"conf={lazy_tele.get('confidence', '?')}, "
                f"{lazy_tele.get('embed_new', 0)}new/"
                f"{lazy_tele.get('embed_cached', 0)}cached)"
            )
            if lazy_tele.get("timed_out"):
                lazy_tag += " · cwd-timeout"
            if lazy_tele.get("db_locked"):
                lazy_tag += " · cwd-db-busy"
            if cross_results:
                lazy_tag += (
                    f" + cross-folder ({cross_tele.get('candidate_roots', 0)} "
                    f"roots · {cross_tele.get('files_seen', 0)} files seen)"
                )
            elif cross_tele.get("timed_out"):
                lazy_tag += " + cross-folder timed out"
            elif cross_tele.get("db_locked"):
                lazy_tag += " + cross-folder db-busy"
        elif lazy and cold_lexical_answered:
            lazy_tag = " · lexical evidence → lazy skipped"
        elif lazy and rg_strong:
            lazy_tag = " · rg strong → lazy skipped"
        elif not lazy:
            lazy_tag = " · --no-lazy"
        else:
            lazy_tag = ""
        if _os.environ.get("SKYGREP_FOOTER_COMPACT") == "1":
            click.echo(
                f"\n[{_wall_elapsed():.3f}s · ripgrep cold-start{proactive_tag}{lazy_tag} · "
                f"intent={intent} · {len(fn_results)} filename + "
                f"{len(rg_cold)} rg + {len(lazy_results)} lazy + "
                f"{len(cross_results)} cross-folder · index {suffix}]"
            )
        else:
            path_parts = ["ripgrep-cold-start"]
            if lazy_results or lazy_tele:
                path_parts.append("lazy-auto")
            if cross_results:
                path_parts.append("cross-folder")
            elif cross_tele.get("timed_out"):
                path_parts.append("cross-folder timed out")
            elif cross_tele.get("db_locked"):
                path_parts.append("cross-folder db-busy")
            if cold_proactive_results:
                path_parts.append("proactive")

            try:
                router_value = (
                    f"{decision.source} -> intent={intent} "
                    f"({float(decision.confidence or 0.0):.2f})"
                )
            except (TypeError, ValueError):
                router_value = f"{decision.source} -> intent={intent}"
            if decision.primary_token:
                router_value += f' · primary_token="{decision.primary_token}"'

            evidence_bits: list[str] = []
            if lazy_tele:
                if "sigma" in lazy_tele:
                    try:
                        evidence_bits.append(
                            f"sigma={float(lazy_tele.get('sigma', 0.0)):.3f}"
                        )
                    except (TypeError, ValueError):
                        pass
                if lazy_tele.get("confidence"):
                    evidence_bits.append(f"conf={lazy_tele.get('confidence')}")
                if lazy_tele.get("embed_new") is not None:
                    evidence_bits.append(
                        f"{lazy_tele.get('embed_new', 0)} new / "
                        f"{lazy_tele.get('embed_cached', 0)} cached"
                    )
            budget_bits: list[str] = []
            if lazy_tele.get("timed_out"):
                budget_bits.append("cwd lazy foreground budget hit")
            if cross_tele.get("timed_out"):
                budget_bits.append("cross-folder foreground budget hit")
            if lazy_tele.get("db_locked"):
                budget_bits.append("cwd lazy skipped: db busy")
            if cross_tele.get("db_locked"):
                budget_bits.append("cross-folder skipped: db busy")

            search_bits = [
                f"{len(fn_results)} filename",
                f"{len(rg_cold)} rg",
                f"{len(lazy_results)} lazy",
                f"{len(cross_results)} cross-folder",
            ]
            if cold_proactive_results:
                search_bits.append(f"{len(cold_proactive_results)} proactive")
            if lazy_tele.get("seeds_total") or lazy_tele.get("seeds_initial"):
                search_bits.append(
                    f"{lazy_tele.get('seeds_total') or lazy_tele.get('seeds_initial')} seeds"
                )
            if cross_tele.get("candidate_roots") is not None:
                search_bits.append(f"{cross_tele.get('candidate_roots', 0)} roots")
            if cross_tele.get("files_seen") is not None:
                search_bits.append(f"{cross_tele.get('files_seen', 0)} files seen")

            rows: list[tuple[str, str]] = [
                ("path", " + ".join(path_parts)),
                ("router", router_value),
            ]
            if evidence_bits:
                rows.append(("evidence", " · ".join(evidence_bits)))
            rows.append(("pool", " · ".join(search_bits)))
            if budget_bits:
                rows.append(("budget", " · ".join(budget_bits)))
            rows.append(("index", suffix))

            click.echo(_ui_done(_wall_elapsed(), "BEST"))
            click.echo(_ui_rows(rows))
        return

    # Index is ready (or auto_index disabled): run the normal pipeline,
    # plus an mtime-based incremental refresh on the way in.
    if auto_index and not machine_context:
        try:
            refreshed = ai.incremental_refresh(
                conn,
                project_root,
                throttle_seconds=ai._refresh_throttle_from_env(),
                quiet=json_output,
                max_foreground_files=ai._foreground_refresh_limit_from_env(),
            )
            if refreshed < 0:
                spawned = ai.spawn_background_index(project_root, db_path)
                if not json_output:
                    note = (
                        "queued"
                        if spawned is not None
                        else "already running"
                    )
                    click.echo(
                        _ui_step(
                            "index",
                            f"background refresh {note} "
                            f"({abs(refreshed)} pending file change(s)).",
                        ),
                        err=True,
                    )
        except Exception as exc:
            logger.warning("auto-refresh failed: %s", exc)

    # L2 one-time symbol extraction. The ``symbols`` table is created by
    # ``init_db`` but is empty on indexes built before L2 — populate it on
    # first use, best-effort so a parser failure or filesystem issue can't
    # block the search itself. Stay quiet on the JSON path so stable
    # consumers (CliRunner, scripts) keep parsing the output cleanly.
    if not machine_context and not _symbols_table_populated(conn):
        try:
            if not json_output:
                click.echo(
                    _ui_step("index", "extracting symbols (one-time, no LLM)"),
                    err=True,
                )
            inserted = populate_symbols(conn, project_root)
            if not json_output:
                click.echo(_ui_step("index", f"{inserted} symbols indexed"), err=True)
        except Exception as exc:
            logger.warning("symbol indexing failed: %s", exc)

    # L4 one-time migration: build the file-export graph if the table is
    # empty. Best-effort; failures here must not block search. Suppressed
    # under ``--json`` so machine-readable callers see clean stdout.
    graph_ready = False
    try:
        row = conn.execute("SELECT COUNT(*) FROM file_graph").fetchone()
        graph_count = row[0] if row else 0
        if graph_count == 0 and not machine_context:
            if not json_output:
                click.echo(
                    _ui_step("index", "building file-export graph (one-time)"),
                    err=True,
                )
            try:
                code_graph.populate_graph_table(conn, project_root)
                graph_ready = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("file-export graph build failed: %s", exc)
        else:
            graph_ready = True
    except sqlite3.OperationalError:
        # Old DB without file_graph table; init_db creates it now, so this
        # path is rare. Silently fall through.
        pass

    status = ai.index_status(conn)
    if status["chunks"] == 0:
        # Empty index is a legitimate state (e.g. after `index --reset` on a
        # directory with no indexable files, or after every file was
        # deleted). Return empty results rather than erroring.
        if json_output:
            click.echo(render_json_results([]))
        else:
            click.echo("[no indexed chunks]")
        return

    if agent_mode == "context" and content:
        from .candidate_recall import (
            build_agent_context_results,
            merge_agent_results,
        )

        recall_query = simplify_router_query(strip_scope_clauses(query) or query)
        agent_scan_root = _agent_lexical_scan_root(
            project_root,
            lexical_root=lexical_root,
            include_patterns=tuple(include_patterns),
        )
        support_per_path = 4 if detail == "full" else 2
        pre_embed_results, pre_embed_recall_telemetry = build_agent_context_results(
            conn,
            recall_query,
            agent_scan_root,
            top_k=top,
            languages=tuple(language),
            include_patterns=tuple(include_patterns),
            exclude_patterns=tuple(exclude_patterns),
            max_paths=max(top * 8, top),
            rg_timeout=0.75,
            support_per_path=support_per_path,
        )
        if pre_embed_results:
            for result in pre_embed_results:
                result["fallback"] = "candidate-recall"
                result["candidate_recall"] = True
            summary = pre_embed_results[0].get("agent_summary", {})
            if summary.get("quality") == "uncertain":
                try:
                    embedder = get_embedder(role="query")
                    _apply_foreground_model_timeout(embedder, model_call_timeout_s)
                    query_embedding = embedder.embed(recall_query)
                    semantic_candidates = set(
                        pre_embed_recall_telemetry.get("path_scores", {}).keys()
                    )
                    semantic_results = search(
                        conn,
                        query_embedding,
                        max(top, top * 2),
                        languages=tuple(language),
                        include_patterns=tuple(include_patterns),
                        exclude_patterns=tuple(exclude_patterns),
                        query_text=recall_query,
                        semantic_only=semantic_only,
                        rerank=False,
                        multi_resolution=True,
                        file_top=max(top * 4, 30),
                        candidate_paths=semantic_candidates or None,
                        rank_by="file",
                    )
                    for result in semantic_results:
                        result["fallback"] = "semantic-escalation"
                        result["candidate_recall_lanes"] = ["semantic-escalation"]
                    if semantic_results:
                        pre_embed_results = merge_agent_results(
                            recall_query,
                            [pre_embed_results, semantic_results],
                            pre_embed_recall_telemetry,
                            top_k=top,
                        )
                except Exception as exc:
                    logger.debug("agent-context semantic escalation skipped: %s", exc)
            if json_output:
                click.echo(render_json_results(pre_embed_results, include_snippet=True))
                return
            for result in pre_embed_results:
                click.echo(
                    render_terminal_result(
                        result,
                        content=True,
                        project_root=str(project_root),
                        detail=detail,
                        ocr=ocr,
                        explain=explain,
                    )
                )
            quality = str(
                pre_embed_results[0]
                .get("agent_summary", {})
                .get("quality", "best")
            ).upper()
            click.echo(_ui_done(_wall_elapsed(), quality))
            click.echo(_ui_rows([
                ("path", "agent-context-hybrid-recall"),
                (
                    "pool",
                    f"{pre_embed_recall_telemetry.get('total_paths', 0)} recalled paths · "
                    f"intent={pre_embed_recall_telemetry.get('intent', 'semantic')}",
                ),
            ]))
            return
        rg_context_results = ai.rg_fallback_results(
            recall_query,
            agent_scan_root,
            top_k=top,
            snippet_lines=10,
            max_candidate_multiplier=2,
            read_budget_s=0.75,
        )
        rg_context_results = _apply_result_boundaries(
            rg_context_results,
            project_root=project_root,
            explicit_scope=explicit_scope,
            include_patterns=tuple(include_patterns),
            exclude_patterns=tuple(exclude_patterns),
        )
        if rg_context_results:
            if json_output:
                click.echo(render_json_results(rg_context_results[:top], include_snippet=True))
                return
            for result in rg_context_results[:top]:
                click.echo(
                    render_terminal_result(
                        result,
                        content=True,
                        project_root=str(project_root),
                        detail=detail,
                        ocr=ocr,
                        explain=explain,
                    )
                )
            click.echo(_ui_done(_wall_elapsed(), "BEST"))
            click.echo(_ui_rows([
                ("path", "agent-context-rg-evidence"),
                ("pool", f"{len(rg_context_results)} rg · semantic-skipped"),
            ]))
            return

    # v0.14.0 hierarchical merge: collect lexical-content shortcut
    # results without short-circuiting. The merged results from all
    # enabled tiers are ranked by intent later — borderline queries
    # always also run cascade so semantic recall is never sacrificed.
    rg_results: list[dict] = []
    rg_elapsed = 0.0
    if (
        rg_shortcut
        and not decision.skip_lexical
        and not agentic
        and not answer
        and not filename_answered
    ):
        rg_start = time.time()
        lexical_query = strip_scope_clauses(query) or query
        rg_hits = ai.lexical_shortcut(
            lexical_query,
            project_root,
            top_k=top,
            allow_content_evidence=explicit_scope,
        )
        rg_elapsed = time.time() - rg_start
        if rg_hits:
            rg_results = rg_hits
            if explain:
                _attach_explain(rg_results, decision)
    lexical_answered = _lexical_evidence_satisfies_depth(
        query,
        rg_results,
        decision,
        detail=detail,
        answer=answer,
        agentic=agentic,
    )
    # 0.5.6 warm-path streaming UX: print filename + ripgrep
    # preliminary matches *before* dispatching the cascade. The
    # cascade can spend tens of seconds on a low-σ-gap escalation
    # to cross-encoder rerank — sitting silent for that long after
    # the user hit Enter is unacceptable. With this block, the
    # user sees their first answer in ≤1 s for any query that has
    # ANY filename or lexical match, even on the warm path. The
    # cascade's semantic refinement still runs and prints below;
    # already-printed paths are deduped against
    # ``early_warm_paths``.
    early_warm_paths: set = set()
    if (not json_output and not answer and not agentic
            and (fn_results or rg_results)):
        warm_preliminary: list = []
        seen_p: set = set()
        for source in (fn_results, rg_results):
            for r in source:
                p = r.get("path", "")
                if not p or p in seen_p:
                    continue
                seen_p.add(p)
                warm_preliminary.append(r)
                if len(warm_preliminary) >= top:
                    break
            if len(warm_preliminary) >= top:
                break
        if warm_preliminary:
            if filename_answered:
                click.echo(_ui_step("filename", "matches:"), err=True)
            elif lexical_answered:
                click.echo(_ui_step("keyword", "matches:"), err=True)
            else:
                click.echo(
                    _ui_step(
                        "seed",
                        "preliminary keyword + filename matches "
                        "(semantic cascade refining):",
                    ),
                    err=True,
                )
            for r in warm_preliminary:
                click.echo(
                    render_terminal_result(
                        r,
                        content=content,
                        project_root=str(project_root),
                        detail=_render_detail_for_result(r, detail, decision),
                        ocr=ocr,
                        explain=explain,
                    )
                )
                early_warm_paths.add(r.get("path", ""))
            click.echo("", err=True)

    # 0.5.6 PROACTIVE UMBRELLA — kick off proactive enhancers
    # (filename_extend etc.) in a BACKGROUND thread NOW, in parallel
    # with cascade. The historical post-cascade call (line 1300+)
    # gated firing on "results are weak", which on warm path meant
    # waiting for the full cascade (potentially 60 s+ on rerank-
    # escalated queries) before filename_extend could even start
    # looking in ``~/Downloads``. The generic case42 benchmark receipt:
    # filename_extend can answer in ~100 ms but was hidden behind
    # 99.7 s of cascade rerank — total wall time 12:50.
    #
    # Conceptual model in `docs/proactive-umbrella-framework.md`:
    # cascade and proactive umbrella subprocesses are SIBLING tiers
    # at t = 0; whichever returns first streams first.
    #
    # We always pass empty `results` here so filename_extend's
    # `should_fire` predicate ("weak results → fire") triggers
    # unconditionally. Already-found paths are deduped against
    # ``early_warm_paths`` and the eventual cascade output before
    # render.
    _proactive_pool = None
    _proactive_fut = None
    _early_proactive_results: list = []
    if (
        not json_output
        and not agentic
        and not filename_answered
        and not fn_results
        and not rg_results
        and not explicit_scope
        and decision.intent == "filename"
    ):
        try:
            from concurrent.futures import ThreadPoolExecutor as _PTPE
            from . import proactive as _proactive_early
            _proactive_pool = _PTPE(max_workers=1)
            _proactive_fut = _proactive_pool.submit(
                _proactive_early.run_enhancers_parallel,
                query, decision, [],  # empty results → enhancers fire
                top_k=top,
                ctx=_proactive_early.ProactiveContext(
                    conn=conn,
                    project_root=project_root,
                    explicit_scope=explicit_scope,
                ),
            )
        except Exception:
            logger.exception(
                "early proactive launch failed; falling back to "
                "post-cascade behaviour"
            )

    # 0.5.6: drain the proactive future BEFORE cascade dispatch.
    # filename_extend is a fast (~100 ms-1 s) `find -iname` call —
    # waiting up to 2 s here means the user sees the proactive
    # answer (e.g. CASE42 PDFs in ~/Downloads) BEFORE cascade even
    # starts, and we know whether to bother showing cascade-only
    # framing if proactive already nailed the answer.
    if _proactive_fut is not None:
        from concurrent.futures import TimeoutError as _PFuturesTimeout
        try:
            _early_proactive_results, _ = _proactive_fut.result(timeout=2.5)
        except _PFuturesTimeout:
            _early_proactive_results = []
            click.echo(
                _ui_step(
                    "proactive",
                    "still searching configured roots (filename_extend running)",
                ),
                err=True,
            )
        if _early_proactive_results:
            try:
                from . import proactive as _proactive_early2
                rendered = _proactive_early2.render_proactive_output(
                    _early_proactive_results,
                    content=content,
                    project_root=str(project_root),
                    detail=detail,
                    ocr=ocr,
                    explain=explain,
                )
                if rendered:
                    click.echo(
                        _ui_step(
                            "proactive",
                            "configured-root filename matches "
                            "(filename_extend, ~100 ms-1 s; pure filename glob, no "
                            "semantic understanding):",
                        ),
                        err=True,
                    )
                    click.echo(rendered)
                    click.echo("", err=True)
            except Exception:
                logger.exception("early proactive render failed")

    # A router may classify the query as filename-like, but it is only
    # allowed to suppress semantic retrieval after the filename tier has
    # produced concrete evidence. Without that evidence, keep the cascade
    # running so a fast intent decision cannot reduce recall.
    answerer = None
    cascade_telemetry: dict | None = None
    candidate_recall_telemetry: dict | None = None
    candidate_recall_results: list[dict] = []
    recovery_state: dict | None = None
    queries = [query]
    if (filename_answered or lexical_answered) and not agentic and not answer:
        if lexical_answered and not filename_answered:
            results = rg_results[:top]
        else:
            results = []
        elapsed = fn_elapsed + rg_elapsed
    else:
        embedder = get_embedder(role="query")
        if machine_context:
            _apply_foreground_model_timeout(embedder, model_call_timeout_s)
        # Intelligent-recovery hook (0.2.2+). Embed the user's query first
        # to get the current embedder dim — that probe gives us
        # ``current_dim`` for free since we'd embed it for the cascade
        # below anyway. ``maybe_start_recovery`` compares the dim against
        # the index's stored embedder fingerprint and spawns a daemon
        # thread to re-embed stale chunks in mtime-DESC order. The thread
        # never blocks the user; the existing ``_filter_to_matching_dim``
        # helper hides stale-dim rows from the cascade so this query
        # returns instantly with whatever has been recovered so far,
        # progressively gaining semantic coverage as the worker commits.
        _probe_vec = embedder.embed(query)
        _probe_dim = len(_probe_vec)
        try:
            recovery_state = maybe_start_recovery(
                db_path, conn, embedder, _probe_dim
            )
        except Exception:
            logger.exception(
                "recovery hook failed; continuing with whatever the index has"
            )
            recovery_state = None
        if recovery_state and recovery_state.get("just_started"):
            stale = recovery_state.get("stale_count", 0)
            eta_min = recovery_state.get("eta_seconds")
            click.echo(
                _ui_step(
                    "index",
                    f"embedder upgraded "
                    f"({recovery_state.get('stored_fingerprint', '?')} -> "
                    f"{recovery_state.get('current_fingerprint', '?')}); "
                    f"re-embedding {stale} stale chunks in the background "
                    f"(mtime-DESC priority). This query falls back to rg "
                    f"+ partial semantic; full semantic resumes "
                    f"progressively as files re-embed.",
                ),
                err=True,
            )
        start = time.time()
        if agentic:
            answerer = get_answerer()
            if machine_context:
                _apply_foreground_model_timeout(answerer, model_call_timeout_s)
            subqueries = answerer.decompose(query, max_queries=max_subqueries)
            for subquery in subqueries:
                if subquery not in queries:
                    queries.append(subquery)
        pool = rerank_pool if rerank_pool is not None else config["rerank_pool"]
        if hyde and not cascade:
            if answerer is None:
                answerer = get_answerer()
                if machine_context:
                    _apply_foreground_model_timeout(answerer, model_call_timeout_s)
            queries = [answerer.hyde(item) for item in queries]
        candidate_paths = None
        if lexical_prefilter:
            from .candidate_recall import (
                candidate_chunk_results,
                recall_candidate_paths,
            )

            prefilter_root = Path(lexical_root) if lexical_root else project_root
            recall_query = simplify_router_query(strip_scope_clauses(query) or query)
            cands, candidate_recall_telemetry = recall_candidate_paths(
                conn,
                recall_query,
                prefilter_root,
                include_patterns=tuple(include_patterns),
                exclude_patterns=tuple(exclude_patterns),
            )
            if len(cands) >= lexical_min_candidates or include_patterns:
                candidate_paths = cands
            # Candidate recall is an additive lane, not a semantic gate. Even
            # when the candidate set is too small to constrain the cascade, we
            # still ask the normal scorer for best evidence from those files so
            # path recall cannot be lost by a later semantic shortlist.
            if cands:
                recall_support_per_path = 0
                if content or answer:
                    if answer or detail == "full":
                        recall_support_per_path = 4
                    elif detail == "standard":
                        recall_support_per_path = 2
                candidate_recall_lexical = candidate_chunk_results(
                    conn,
                    recall_query,
                    cands,
                    top_k=max(top, top * 2),
                    languages=tuple(language),
                    include_patterns=tuple(include_patterns),
                    exclude_patterns=tuple(exclude_patterns),
                    path_scores=candidate_recall_telemetry.get("path_scores", {}),
                    path_lanes=candidate_recall_telemetry.get("path_lanes", {}),
                    support_per_path=recall_support_per_path,
                )
                candidate_recall_semantic = search(
                    conn,
                    _probe_vec,
                    max(top, top * 2),
                    languages=tuple(language),
                    include_patterns=tuple(include_patterns),
                    exclude_patterns=tuple(exclude_patterns),
                    query_text=recall_query,
                    semantic_only=semantic_only,
                    rerank=False,
                    multi_resolution=False,
                    candidate_paths=cands,
                    rank_by="file",
                )
                candidate_recall_results = merge_results(
                    [candidate_recall_lexical, candidate_recall_semantic],
                    max(top, top * 3),
                )
                path_lanes = candidate_recall_telemetry.get("path_lanes", {})
                for result in candidate_recall_results:
                    result["fallback"] = "candidate-recall"
                    result["candidate_recall"] = True
                    result["candidate_recall_lanes"] = path_lanes.get(
                        result.get("path", ""),
                        [],
                    )

        result_groups: list[list[dict]] = []
        if candidate_recall_results:
            result_groups.append(candidate_recall_results)
        for item in queries:
            query_embedding = embedder.embed(item)
            if cascade:
                if answerer is None:
                    answerer = get_answerer()
                if machine_context:
                    _apply_foreground_model_timeout(answerer, model_call_timeout_s)
                # 0.5.6: hard 30 s wall-clock timeout on cascade.
                # On vocabulary-mismatch queries (the "case42" / generic
                # filename term in a code repo case) the σ-adaptive gate flips to
                # cross-encoder rerank which can run 60–100 s. The user
                # has already seen the proactive umbrella's answer (e.g.
                # filename_extend hits in ~/Downloads) at ≤ 2 s — there
                # is no UX benefit to forcing the user to wait 60+ s for
                # a cascade that already failed σ-validation. After the
                # timeout we proceed with whatever cascade had on the
                # cheap path (often empty) and the script exits.
                from concurrent.futures import (
                    ThreadPoolExecutor as _CTPE,
                    TimeoutError as _CFT,
                )
                # The cascade runs in a worker thread so we can apply
                # a 30 s wall-clock timeout. SQLite forbids reusing a
                # connection across threads, so we open the
                # connection INSIDE the worker function — the conn
                # is local to the worker thread. SQLite serialises
                # concurrent readers transparently, so the main
                # thread's existing ``conn`` is unaffected.
                def _cascade_in_worker(_db_path=db_path):
                    _wconn = init_db(_db_path)
                    try:
                        return cascade_search(
                            _wconn,
                            query_embedding,
                            query_text=item,
                            embedder=embedder,
                            answerer=answerer,
                            top_k=max(top, top * 2),
                            candidate_paths=candidate_paths,
                            tau=cascade_tau,
                            languages=tuple(language),
                            include_patterns=tuple(include_patterns),
                            exclude_patterns=tuple(exclude_patterns),
                        )
                    finally:
                        try:
                            _wconn.close()
                        except Exception:
                            pass

                _cpool = _CTPE(max_workers=1)
                _cfut = _cpool.submit(_cascade_in_worker)
                try:
                    cascade_results, cascade_telemetry = _cfut.result(
                        timeout=cascade_timeout_s
                    )
                except _CFT:
                    _cfut.cancel()
                    cascade_results = []
                    cascade_telemetry = {
                        "path": "cascade-timeout",
                        "timed_out": True,
                        "gap": 0.0,
                        "tau": 0.0,
                    }
                    if not json_output:
                        click.echo(
                            _ui_step(
                                "budget",
                                f"cascade timed out at {cascade_timeout_s:g} s - "
                                "top-K above (filename_extend / preliminary "
                                "cascade / cross-folder) is the answer; cascade "
                                "was in sigma-low rerank, unlikely to add value",
                            ),
                            err=True,
                        )
                except Exception as _cexc:  # noqa: BLE001
                    logger.warning("cascade error in thread: %s", _cexc)
                    cascade_results = []
                    cascade_telemetry = None
                finally:
                    _cpool.shutdown(wait=False, cancel_futures=True)
                result_groups.append(cascade_results)
                continue
            result_groups.append(
                search(
                    conn,
                    query_embedding,
                    max(top, top * 2),
                    languages=tuple(language),
                    include_patterns=tuple(include_patterns),
                    exclude_patterns=tuple(exclude_patterns),
                    query_text=item,
                    semantic_only=semantic_only,
                    rerank=rerank,
                    rerank_pool=pool,
                    rerank_model=rerank_model,
                    multi_resolution=multi_resolution,
                    file_top=file_top,
                    candidate_paths=candidate_paths,
                    rank_by=rank_by,
                )
            )
        merge_width = max(top, top * 3) if candidate_recall_results else top
        results = merge_results(result_groups, merge_width)
        elapsed = time.time() - start

    # 0.5.3 warm-path low-confidence augmentation criterion:
    # when the cascade ran and reports a small ``top1 - top2`` gap
    # (its own σ-adaptive confidence signal), the answer may not
    # live in cwd. Fire ``lazy_explore_cross_folder`` against the
    # SKYGREP_PROACTIVE_DIRS roots and include those hits as
    # augmentation. Cascade remains primary; cross-folder is
    # appended after we've already shown the user the preliminary
    # cascade matches (0.5.6 streaming).
    will_fire_warm_cross = (
        lazy and cascade and len(queries) == 1
        and 'cascade_telemetry' in dir() and cascade_telemetry is not None
        and not agentic
        and not explicit_scope
        and not results
        and (cascade_telemetry.get("gap") is not None)
        and (cascade_telemetry.get("tau") is not None)
        and (cascade_telemetry.get("gap") < cascade_telemetry.get("tau"))
    )
    warm_cross_results: list = []
    warm_cross_tele: dict = {}
    warm_cross_elapsed = 0.0

    # v0.14.0 hierarchical merge — preliminary version (cascade
    # semantic + filename + lexical, no cross-folder yet).
    intent = decision.intent
    if _semantic_filename_anchor_should_lead(decision, fn_results):
        results = _merge_sources_preferring_depth(
            (fn_results, results, rg_results),
            top=top,
        )
    else:
        results = merge_tiers(
            filename=fn_results,
            lexical=rg_results,
            semantic=results,
            intent=intent,
            top_k=top,
        )
    results = _apply_adaptive_metadata_ranking(results, decision)
    results = _apply_result_boundaries(
        results,
        project_root=project_root,
        explicit_scope=explicit_scope,
        include_patterns=tuple(include_patterns),
        exclude_patterns=tuple(exclude_patterns),
    )
    if explain:
        _attach_explain(results, decision)

    # 0.5.6 streaming UX: when warm cross-folder is about to fire
    # (cascade was uncertain, sibling-folder search incoming), also
    # print the cascade's best-guess top-K *now* (in addition to
    # the earlier fn / rg preliminary block) so the user sees the
    # full pre-cross-folder picture before the 8 s sibling-folder
    # window. ``early_warm_paths`` was already populated by the
    # earlier fn/rg block; we extend it with cascade hits that
    # weren't already shown so the cross-folder augmentation
    # block below dedupes correctly.
    if will_fire_warm_cross and not json_output and not answer:
        cascade_only = [
            r for r in results
            if r.get("path", "") and r.get("path", "") not in early_warm_paths
        ]
        if cascade_only:
            click.echo(
                _ui_step(
                    "cascade",
                    "preliminary matches "
                    "(low confidence - also searching sibling folders):",
                ),
                err=True,
            )
            for r in cascade_only:
                click.echo(
                    render_terminal_result(
                        r,
                        content=content,
                        project_root=str(project_root),
                        detail=_render_detail_for_result(r, detail, decision),
                        ocr=ocr,
                        explain=explain,
                    )
                )
                early_warm_paths.add(r.get("path", ""))
            click.echo("", err=True)

    # Now actually fire the cross-folder pass — wrapped in a hard
    # 8 s wall-clock deadline so a slow ``~/Documents`` walk can't
    # block the full search behind the user's 3-5 s "first answer"
    # expectation. The preliminary cascade matches are already on
    # screen by this point; if cross-folder doesn't finish in 8 s we
    # just print "(timed out)" and call it done.
    if will_fire_warm_cross:
        from concurrent.futures import (
            ThreadPoolExecutor as _TPE,
            TimeoutError as _FuturesTimeout,
        )
        from . import lazy_indexer as _LZ
        _cross_embedder = get_embedder(role="query")
        _warm_cross_query = simplify_router_query(strip_scope_clauses(query) or query)
        try:
            _warm_cross_embed_timeout = max(
                0.5,
                float(_os.environ.get("SKYGREP_WARM_CROSS_EMBED_TIMEOUT_S", "4")),
            )
            setattr(_cross_embedder, "request_timeout_s", _warm_cross_embed_timeout)
            setattr(_cross_embedder, "batch_timeout_s", _warm_cross_embed_timeout)
        except ValueError:
            pass
        try:
            _warm_cross_seed_budget = max(
                1, int(_os.environ.get("SKYGREP_WARM_CROSS_SEED_BUDGET", "5"))
            )
        except ValueError:
            _warm_cross_seed_budget = 5
        _t_cross = time.time()
        # 0.5.7: open a SQLite connection INSIDE the worker thread.
        # Same fix as 0.5.6 cascade-in-worker-thread and the
        # cold+wrong-folder branch above. Passing the main-thread
        # ``conn`` into the worker triggered "SQLite objects
        # created in a thread can only be used in that same thread"
        # and silently zero-ed every warm cross-folder call.
        def _warm_cross_in_worker(_db_path=db_path):
            _wconn = init_db(_db_path)
            try:
                return _LZ.lazy_explore_cross_folder(
                    _wconn, _warm_cross_query,
                    embedder=_cross_embedder,
                    top_k=top,
                    seed_budget=_warm_cross_seed_budget,
                    progress=None if json_output else _LZ._stderr_progress,
                )
            finally:
                try:
                    _wconn.close()
                except Exception:
                    pass

        try:
            _xpool = _TPE(max_workers=1)
            _fut = _xpool.submit(_warm_cross_in_worker)
            try:
                warm_cross_results, warm_cross_tele = _fut.result(timeout=8.0)
            except _FuturesTimeout:
                warm_cross_tele = {
                    "path": "lazy-cross-folder",
                    "timed_out": True,
                }
                if not json_output:
                    click.echo(
                        _ui_step(
                            "budget",
                            "sibling-folder search timed out at 8 s - "
                            "skipping (cascade matches above are the answer)",
                        ),
                        err=True,
                    )
            finally:
                _xpool.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("warm cross-folder lazy failed: %s", exc)
        warm_cross_elapsed = time.time() - _t_cross
        elapsed += warm_cross_elapsed

    # 0.5.3: append warm-path cross-folder augmentation if cascade
    # was uncertain. Cross-folder is appended after merge_tiers
    # (cascade primary; sibling-folder hits added as long as room
    # remains in top-K), deduped by path so a result already
    # returned by cascade isn't double-printed.
    if warm_cross_results:
        _seen_paths = {r.get("path", "") for r in results}
        for r in warm_cross_results:
            if len(results) >= top:
                break
            p = r.get("path", "")
            if not p or p in _seen_paths:
                continue
            results.append(r)
            _seen_paths.add(p)
        results = _apply_adaptive_metadata_ranking(results, decision)
        results = _apply_result_boundaries(
            results,
            project_root=project_root,
            explicit_scope=explicit_scope,
            include_patterns=tuple(include_patterns),
            exclude_patterns=tuple(exclude_patterns),
        )
        if explain:
            _attach_explain(results, decision)
    # 0.5.8 explainability: print the cascade-lane summary right before
    # the final render block. The router header was printed earlier
    # (right after the routing decision); this is the second half of
    # the explanation — which retrieval lane actually answered.
    if explain and not json_output and not answer:
        _lane = _format_lane_explain(
            cascade_telemetry if 'cascade_telemetry' in dir() else None
        )
        if _lane:
            click.echo(_lane, err=True)
    if json_output:
        _augment_filename_content_for_machine(
            results, query, decision, detail=detail, ocr=ocr,
        )
        if machine_context:
            evidence_floor = _env_float(
                "SKYGREP_AGENT_MIN_EVIDENCE_SCORE", 0.50, minimum=0.0
            )
            results = _filter_low_evidence_machine_results(
                results, query, min_score=evidence_floor
            )
        click.echo(render_json_results(results, include_snippet=content))
        return
    if answer:
        _augment_filename_content_for_machine(
            results, query, decision, detail=detail, ocr=ocr,
            for_answer=True,
        )
        if answerer is None:
            answerer = get_answerer()
        synthesized = answerer.answer(query, results)
        click.echo(synthesized)
        click.echo("\nSources:")
        for result in results:
            click.echo(render_compact_source(result))
        click.echo(f"\n[Answer completed in {_wall_elapsed():.3f}s]")
        return
    # 0.5.6 streaming UX: if the warm-path streaming block already
    # printed the preliminary cascade matches, only echo NEW results
    # added by the cross-folder pass (deduped against
    # ``early_warm_paths``). Otherwise (rg-strong / no cross-folder
    # / json) render the full ranked top-K.
    if (filename_answered or lexical_answered) and early_warm_paths:
        # The fast-path answer already streamed above. Do not print the
        # generic "sibling-folder search added no new matches" message;
        # no sibling-folder semantic pass ran on this path.
        pass
    elif early_warm_paths:
        new_warm = [
            r for r in results
            if r.get("path", "") and r.get("path", "") not in early_warm_paths
        ]
        if new_warm:
            warm_label = (
                _ui_step("refine", "matches from sibling-folder semantic search:")
                if warm_cross_results
                else _ui_step("refine", "matches from semantic search:")
            )
            click.echo(
                warm_label,
                err=True,
            )
            for r in new_warm:
                click.echo(
                    render_terminal_result(
                        r,
                        content=content,
                        project_root=str(project_root),
                        detail=_render_detail_for_result(r, detail, decision),
                        ocr=ocr,
                        explain=explain,
                    )
                )
        else:
            click.echo(
                _ui_step(
                    "refine",
                    "sibling-folder search added no new matches "
                    "(top-K above is the final answer).",
                ),
                err=True,
            )
    else:
        for r in results:
            click.echo(
                render_terminal_result(
                    r,
                    content=content,
                    project_root=str(project_root),
                    detail=_render_detail_for_result(r, detail, decision),
                    ocr=ocr,
                    explain=explain,
                )
            )

    # ``path=`` is the headline routing decision the user actually cares
    # about: which retrieval strategy answered this specific query.
    cascade_was_skipped = filename_answered or lexical_answered
    if filename_answered:
        path_label = "filename-lookup"
    elif lexical_answered:
        path_label = "lexical-exact"
    else:
        path_label = "cascade"
    if cascade and cascade_telemetry is not None:
        path_label = cascade_telemetry.get("path") or (
            "cosine-cheap" if cascade_telemetry.get("early_exit") else "cosine-escalated-rerank"
        )
    # Recovery state — read fresh from the metadata table even if the
    # search-cmd-time snapshot is stale by the time results print.
    live_recovery = get_recovery_state(conn) if recovery_state is not None else None
    recovery_footer = render_recovery_footer(live_recovery) if live_recovery else None
    quality = "DEGRADED-recovery" if recovery_footer else "BEST"

    # ``SKYGREP_FOOTER_COMPACT=1`` keeps the legacy single-line format
    # for users / scripts that prefer terse output. Default is the
    # 0.2.5 hierarchical multi-line footer with category groups so
    # the user can scan path / router / evidence / pool / index
    # vertically rather than parsing one long ``·``-separated line.
    compact = _os.environ.get("SKYGREP_FOOTER_COMPACT") == "1"
    wall_elapsed = _wall_elapsed()

    if compact:
        # Legacy footer — preserved verbatim for any tooling that grepped
        # the old format. New behaviour is the hierarchical block below.
        parts: list[str] = [f"{wall_elapsed:.3f}s", f"path={path_label}"]
        parts.append(
            f"router={decision.source} · intent={intent} "
            f"({decision.confidence:.2f}) · "
            f"{len(fn_results)} filename + {len(rg_results)} lexical"
        )
        if cascade and cascade_telemetry is not None:
            gap = cascade_telemetry.get('gap', 0)
            tau = cascade_telemetry.get('tau', 0)
            tau_mode = cascade_telemetry.get('tau_mode', 'static')
            if cascade_telemetry.get("early_exit"):
                parts.append(
                    f"σ-gap={gap:.4f} ≥ τ={tau:.4f} ({tau_mode}) → high-confidence early-exit"
                )
            else:
                parts.append(
                    f"σ-gap={gap:.4f} < τ={tau:.4f} ({tau_mode}) → escalated to rerank"
                )
        parts.append(f"index {ai.index_age_human(conn)} · {status['files']} files")
        if _symbols_table_populated(conn):
            parts.append("L2 symbols on")
        if graph_ready:
            parts.append("graph prior on")
        if recovery_footer:
            parts.append(recovery_footer)
        parts.append(f"quality={quality}")
        click.echo("\n[" + " · ".join(parts) + "]")
    else:
        # Hierarchical footer. One rail terminator with elapsed + quality,
        # then indented category rows. Each row is a single semantic group
        # so the user can read top-down without parsing separators.
        path_detail = ""
        if cascade and cascade_telemetry is not None:
            if cascade_telemetry.get("early_exit"):
                path_detail = " (high-confidence early-exit)"
            else:
                path_detail = " (escalated to rerank)"
        rows: list[tuple[str, str]] = []
        rows.append(("path", f"{path_label}{path_detail}"))
        rows.append((
            "router",
            f"{decision.source} -> intent={intent} ({decision.confidence:.2f})",
        ))
        if cascade and cascade_telemetry is not None:
            gap = cascade_telemetry.get('gap', 0)
            tau = cascade_telemetry.get('tau', 0)
            tau_mode = cascade_telemetry.get('tau_mode', 'static')
            cmp = "≥" if cascade_telemetry.get("early_exit") else "<"
            rows.append((
                "evidence",
                f"σ-gap={gap:.4f} {cmp} τ={tau:.4f} ({tau_mode})",
            ))
        pool_pieces = [
            f"{len(fn_results)} filename",
            f"{len(rg_results)} lexical",
        ]
        if candidate_recall_telemetry:
            pool_pieces.append(
                f"{candidate_recall_telemetry.get('total_paths', 0)} recall"
            )
        if cascade and not cascade_was_skipped:
            pool_pieces.append("cascade")
        elif cascade_was_skipped:
            pool_pieces.append("cascade-skipped")
        pool_value = " + ".join(pool_pieces[:2])
        if len(pool_pieces) > 2:
            pool_value += " · " + pool_pieces[2]
        rows.append(("pool", pool_value))
        index_pieces = [f"{ai.index_age_human(conn)} ago", f"{status['files']} files"]
        index_extras: list[str] = []
        if _symbols_table_populated(conn):
            index_extras.append("L2 symbols")
        if graph_ready:
            index_extras.append("graph prior")
        if index_extras:
            index_pieces.append(" + ".join(index_extras))
        if any(r.get("graph_tiebreak") for r in results) and len(results) >= 2:
            tie_gap = float(results[0].get("score", 0)) - float(results[1].get("score", 0))
            index_pieces.append(f"tied (Δ={tie_gap:.3f})")
        rows.append(("index", " · ".join(index_pieces)))
        if recovery_footer:
            # The recovery_footer already returns a "key=value · key=value"
            # string ("recovery=in-progress chunks=N/T coverage=N% ETA=Nm");
            # strip the leading "recovery=in-progress " so the row reads
            # cleanly under the "recovery" label.
            cleaned = recovery_footer.replace("recovery=in-progress", "in-progress")
            rows.append(("recovery", cleaned))
        # Render
        click.echo(_ui_done(wall_elapsed, quality))
        click.echo(_ui_rows(rows))

    # Proactive enhancement framework (0.2.7+). Runs registered
    # enhancers (filename_extend, ...) IN PARALLEL with a hard
    # ``SKYGREP_PROACTIVE_BUDGET_MS`` cap (default 500 ms) so normal
    # queries pay zero extra latency — only the should-fire-eligible
    # enhancers get scheduled, and over-budget ones are cancelled by
    # the thread pool. See ``docs/PRINCIPLES.md`` Principle 6
    # ("Proactive over Passive") for the contract every enhancer
    # must honour.
    proactive_results: list = []
    # 0.5.6: if the early parallel proactive launch (before cascade)
    # already produced results, reuse them — don't double-fire.
    # ``_early_proactive_results`` was filled with whatever
    # filename_extend / etc. found within the 2.5 s pre-cascade
    # drain. Otherwise (timed out before cascade dispatch, or
    # early launch was disabled), do the historical post-cascade
    # call so behaviour is preserved for the json / agentic /
    # answer paths.
    if _early_proactive_results:
        proactive_results = _early_proactive_results
    elif not json_output:
        try:
            from . import proactive as _proactive

            proactive_results, _proactive_telemetry = (
                _proactive.run_enhancers_parallel(
                    query, decision, results, top_k=top,
                    ctx=_proactive.ProactiveContext(
                        conn=conn,
                        project_root=project_root,
                        explicit_scope=explicit_scope,
                    ),
                )
            )
            if proactive_results:
                rendered = _proactive.render_proactive_output(
                    proactive_results,
                    content=content,
                    project_root=str(project_root),
                    detail=detail,
                    ocr=ocr,
                    explain=explain,
                )
                if rendered:
                    click.echo(rendered)
        except Exception:
            logger.exception("proactive enhancer runner failed; ignoring")
    if _proactive_pool is not None:
        _proactive_pool.shutdown(wait=False)

    # Intelligent CLI hint — low-confidence result quality (0.2.4+).
    # When top-1 cosine and σ-gap are both below floor, the result is
    # in the noise band; offer a recovery menu rather than letting the
    # user quit thinking the tool failed. Hint is silenced when the
    # query produced no results AND was already routed through rg
    # fallback (the user will know they need to refine), and also when
    # the proactive framework already surfaced extra hits — the user
    # has more to look at, no need to also nag.
    if (
        not hints_disabled()
        and not json_output
        and not proactive_results
        and not cascade_was_skipped
    ):
        _quality_hint = assess_result_quality(results, cascade_telemetry)
        if _quality_hint:
            click.echo(_quality_hint, err=True)

    # First-run nudge: encourage the user to register skylakegrep with any
    # detected LLM CLIs once. Suppressed under --json (machine consumers
    # parsing the output), in non-TTY contexts (agents piping output), and
    # after `skygrep setup` has been run at least once.
    if (
        not json_output
        and sys.stdout.isatty()
        and not integrations_mod.is_setup_done()
    ):
        msg = integrations_mod.first_run_banner_message()
        if msg:
            click.echo(msg, err=True)

    # Optional background auto-enrich. Off by default; users opt in by
    # exporting ``SKYGREP_AUTO_ENRICH=yes``. We only spawn when the index is
    # actually ready — never alongside the rg-fallback path — so we don't
    # contend with the still-running first-time indexer for Ollama.
    if (
        ready
        and _os.environ.get("SKYGREP_AUTO_ENRICH") == "yes"
        and enrich_mod.count_pending(conn) > 0
    ):
        try:
            import subprocess as _subprocess

            log = db_path.with_suffix(db_path.suffix + ".enrich.log")
            env = dict(_os.environ)
            env["SKYGREP_DB_PATH"] = str(db_path)
            _subprocess.Popen(
                [sys.executable, "-m", "skylakegrep.src.cli", "enrich", "--max", "50"],
                cwd=str(project_root),
                env=env,
                stdout=open(log, "ab", buffering=0),
                stderr=_subprocess.STDOUT,
                stdin=_subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as exc:  # pragma: no cover — best-effort spawn
            logger.warning("auto-enrich spawn failed: %s", exc)


@cli.command()
@click.argument("path", default=".")
@click.option("--interval", "-i", default=5, help="Check interval in seconds")
def watch(path: str, interval: int):
    """Continuously index a directory: poll mtimes, reindex changed files."""

    config = get_config()
    db_path = config["db_path"]
    conn = init_db(db_path)
    embedder = get_embedder()
    indexed_files = get_indexed_files(conn)

    click.echo(f"Watching {path} for changes (Ctrl+C to stop)")
    while True:
        try:
            root = Path(path)
            files = collect_indexable_files(root)
            deleted_files = delete_missing_files(conn, {str(f) for f in files}, root)
            for deleted_file in deleted_files:
                indexed_files.pop(deleted_file, None)
                click.echo(f"  Deleted: {deleted_file}")
            files_to_refresh: list[Path] = []
            refresh_kinds: dict[str, str] = {}
            refresh_mtimes: dict[str, float] = {}
            for f in files:
                f_str = str(f)
                current_mtime = f.stat().st_mtime
                if f_str in indexed_files:
                    if current_mtime > indexed_files[f_str]:
                        files_to_refresh.append(f)
                        refresh_kinds[f_str] = "Updated"
                        refresh_mtimes[f_str] = current_mtime
                else:
                    files_to_refresh.append(f)
                    refresh_kinds[f_str] = "Added"
                    refresh_mtimes[f_str] = current_mtime
            for f, chunks in embed_file_chunks_batched(
                files_to_refresh,
                embedder,
                root=root,
            ):
                f_str = str(f)
                # Delete once even when the updated file is now empty, or the
                # previous chunks would remain searchable forever.
                delete_file_chunks(conn, f_str)
                if chunks:
                    store_chunks_batch(conn, chunks)
                indexed_files[f_str] = refresh_mtimes[f_str]
                click.echo(f"  {refresh_kinds[f_str]}: {f}")
            if deleted_files or files_to_refresh:
                populate_file_embeddings(conn)
            time.sleep(interval)
        except KeyboardInterrupt:
            click.echo("\nStopping watch mode")
            break


@cli.command()
@click.option("--host", default="127.0.0.1", help="Host to bind the daemon on")
@click.option("--port", default=7878, type=int, help="Port to bind the daemon on")
@click.option(
    "--warm-reranker/--no-warm-reranker",
    default=False,
    help="Warm the optional cross-encoder in the background after the daemon is ready",
)
def serve(host: str, port: int, warm_reranker: bool):
    """Run a low-latency local search daemon."""

    from .server import serve as _serve

    _serve(host=host, port=port, warm_reranker=warm_reranker)


@cli.command()
def stats():
    """Print chunk and file counts for the current project's index."""

    config = get_config()
    db_path = config["db_path"]
    click.echo(f"DB:           {db_path}")
    click.echo(f"Project root: {cfg_mod.project_root()}")
    if not db_path.exists():
        click.echo("No index yet. Run a query to auto-index, or `skygrep index .`.")
        return
    conn = init_db(db_path)
    row = conn.execute("SELECT COUNT(*), COUNT(DISTINCT file) FROM chunks").fetchone()
    click.echo(f"Total chunks: {row[0]}")
    click.echo(f"Total files:  {row[1]}")
    enriched, total = enrich_mod.count_enriched(conn)
    if total:
        pct = 100.0 * enriched / total
        click.echo(f"Enriched:     {enriched} / {total} ({pct:.1f}%)")
    snap = auto_index.index_status(conn)
    if snap["last_full_index_at"]:
        click.echo(f"Last full:    {auto_index._human_age(time.time() - snap['last_full_index_at'])}")
    if snap["last_refresh_at"]:
        click.echo(f"Last refresh: {auto_index._human_age(time.time() - snap['last_refresh_at'])}")


@cli.command()
@click.option("--max", "max_chunks", type=int, default=None,
              help="Stop after this many chunks (default: run to completion).")
@click.option("--batch", default=5, help="Chunks per progress line.")
def enrich(max_chunks, batch):
    """Run doc2query enrichment over the current project's index.

    For each chunk that has not been enriched yet, call the local LLM to
    write a one-sentence description, append it to the chunk text, and
    re-embed. Resumable — safe to Ctrl+C and re-run.
    """

    config = get_config()
    db_path = config["db_path"]
    if not db_path.exists():
        click.echo(
            "No index yet. Run `skygrep index .` (or just query the project) "
            "before enrichment.",
            err=True,
        )
        return
    conn = init_db(db_path)
    pending_before = enrich_mod.count_pending(conn)
    if pending_before == 0:
        click.echo("All chunks are already enriched.")
        return
    click.echo(
        f"Enriching up to {max_chunks if max_chunks is not None else pending_before} "
        f"chunk(s) ({pending_before} pending)..."
    )
    n = enrich_mod.enrich_pending_chunks(
        conn,
        max_chunks=max_chunks,
        batch_size=batch,
    )
    enriched, total = enrich_mod.count_enriched(conn)
    pct = 100.0 * enriched / total if total else 0.0
    click.echo(
        f"Enriched {n} chunk(s) this run · {enriched} / {total} ({pct:.1f}%) total."
    )


@cli.command()
def doctor():
    """Health check: probe Ollama, list models, summarise the project index."""

    refreshed_setup = _auto_refresh_setup_snippets()
    config = get_config()
    report = bootstrap.doctor_report(config["ollama_url"])
    pad = lambda label: f"  {label:<26}"  # noqa: E731
    click.echo("skygrep doctor")
    click.echo(f"{pad('Version')}{__version__}")
    click.echo(f"{pad('Python')}{sys.executable}")
    invoked = str(Path(sys.argv[0]).resolve()) if sys.argv and sys.argv[0] else "(unknown)"
    click.echo(f"{pad('CLI invoked')}{invoked}")
    click.echo(f"{pad('CLI on PATH')}{shutil.which('skygrep') or '(not on PATH)'}")
    if report["ollama"]["ok"]:
        click.echo(f"{pad('Ollama runtime')}✓ {report['ollama']['url']}")
    else:
        click.echo(f"{pad('Ollama runtime')}× {report['ollama']['url']}")
        click.echo(f"  → {report['ollama']['error']}")
        click.echo(f"\n{bootstrap.OLLAMA_INSTALL_HINT}")
        sys.exit(1)
    for entry in report["models"]:
        mark = "✓" if entry["present"] else "×"
        click.echo(f"{pad(entry['role'].title() + ' model')}{mark} {entry['name']}")
        if not entry["present"]:
            click.echo(f"  → run: ollama pull {entry['name']}")
    keep_alive = report.get("keep_alive") or "(default)"
    click.echo(f"{pad('Ollama keep_alive')}{keep_alive}")
    # Project index status.
    db_path = config["db_path"]
    if db_path.exists():
        conn = init_db(db_path)
        snap = auto_index.index_status(conn)
        click.echo(
            f"{pad('Project index')}✓ {snap['files']} files / {snap['chunks']} chunks"
            + (f" · refreshed {auto_index._human_age(time.time() - snap['last_refresh_at'])}" if snap["last_refresh_at"] else "")
        )
        enriched, total = enrich_mod.count_enriched(conn)
        if total:
            pct = 100.0 * enriched / total
            click.echo(f"{pad('Enriched chunks')}{enriched} / {total} ({pct:.1f}%)")
        click.echo(f"{pad('Index DB')}{db_path}")
    else:
        click.echo(f"{pad('Project index')}× not yet built — run a query to auto-index, or `skygrep index .`")
        click.echo(f"{pad('Would write to')}{db_path}")
    # Reranker presence is a soft check. Do not import the package here:
    # importing sentence-transformers also imports PyTorch and can turn a
    # lightweight health check into a multi-minute runtime startup.
    if importlib.util.find_spec("sentence_transformers") is not None:
        click.echo(f"{pad('Reranker (optional)')}✓ sentence-transformers installed")
    else:
        click.echo(f"{pad('Reranker (optional)')}— install: pip install 'skylakegrep[rerank]'")
    click.echo(f"{pad('Project root')}{cfg_mod.project_root()}")
    # LLM-CLI integrations registered via ``skygrep setup``.
    detected = [i for i in integrations_mod.all_integrations() if i.is_detected()]
    if detected:
        if refreshed_setup:
            names = ", ".join(i.name for i in refreshed_setup)
            click.echo(f"{pad('LLM CLI rules')}✓ refreshed managed snippet(s): {names}")
        for i in detected:
            mark = "✓" if i.is_registered() else "—"
            click.echo(f"{pad(f'LLM CLI: {i.name}')}{mark} {'registered' if i.is_registered() else 'not registered (run `skygrep setup`)'}")


@cli.command()
@click.option("--list", "list_only", is_flag=True, help="List detected LLM CLIs and registration state, then exit.")
@click.option("--check", is_flag=True, help="Check whether managed setup snippets are current; exits non-zero when a registered snippet is stale or broken.")
@click.option("--uninstall", is_flag=True, help="Remove all snippets previously written by `skygrep setup`.")
@click.option("--skip", is_flag=True, help="Mark setup as done without registering anything (suppresses the first-run banner).")
@click.option("--yes", "-y", is_flag=True, help="Auto-confirm every detected integration without an interactive prompt.")
@click.pass_context
def setup(ctx, list_only: bool, check: bool, uninstall: bool, skip: bool, yes: bool):
    """Register skylakegrep as preferred semantic search with installed LLM CLIs.

    Detects Claude Code, Codex, OpenCode, Gemini CLI, and Cursor on
    your machine and offers to write a tiny markdown snippet into each
    one's user-level instructions file. The snippet hints to the agent
    that it should prefer ``skygrep`` for natural-language code search and
    fall back to ``rg`` otherwise.

    Re-running ``skygrep setup`` refreshes existing managed snippets when
    the shipped agent guidance changes. Use ``--list`` or ``--check`` to
    inspect without modifying. Use ``--uninstall`` to remove every snippet previously
    written. Use ``--skip`` to suppress the first-run banner without
    registering anything.
    """
    items = integrations_mod.all_integrations()

    if list_only:
        click.echo("Detected LLM CLIs (run `skygrep setup` to register):")
        for i in items:
            mark = "✓" if i.is_detected() else "·"
            status = i.registration_status()
            reg = f" [{status}]" if i.is_registered() else ""
            click.echo(f"  {mark} {i.name:<14} {i.description}{reg}")
            click.echo(f"      config: {i.config_path}")
        return

    if check:
        click.echo("skygrep setup instruction status:")
        stale = False
        for i in items:
            status = i.registration_status()
            if status in {"stale", "broken"}:
                stale = True
            mark = "✓" if status == "current" else ("!" if status in {"stale", "broken"} else "·")
            click.echo(f"  {mark} {i.name:<14} {status:<7} {i.config_path}")
        if stale:
            click.echo("\nRun `skygrep setup` to refresh stale managed snippets.")
            ctx.exit(1)
        return

    if uninstall:
        n_removed = 0
        for i in items:
            if i.unregister():
                click.echo(f"  ✓ removed snippet from {i.name} ({i.config_path})")
                n_removed += 1
        click.echo(f"\nDone. Removed {n_removed} integration(s).")
        return

    if skip:
        integrations_mod.mark_setup_done()
        click.echo("Marked setup done. The first-run banner will no longer appear.")
        click.echo("Re-run `skygrep setup` any time to register integrations.")
        return

    detected = [i for i in items if i.is_detected()]
    if not detected:
        click.echo("No supported LLM CLIs detected.")
        click.echo("\nWe look for:")
        for i in items:
            click.echo(f"  · {i.name} — {i.description}")
        click.echo("\nIf you do have one of these installed but it wasn't detected, "
                   "either ensure its binary is on PATH or its config dir exists "
                   "before running `skygrep setup` again.")
        integrations_mod.mark_setup_done()
        return

    click.echo("Detected the following LLM CLIs on your machine:\n")
    for i in detected:
        already = " [already registered]" if i.is_registered() else ""
        click.echo(f"  ✓ {i.name:<14} → {i.config_path}{already}")
    click.echo()
    if yes:
        click.echo("--yes set; registering all without prompting.\n")

    n_changed = 0
    for i in detected:
        if i.is_registered():
            try:
                wrote = i.register()
                if wrote:
                    click.echo(f"  ✓ updated snippet in {i.config_path}")
                    n_changed += 1
                else:
                    click.echo(f"  · {i.name} already had the current snippet (no change)")
            except OSError as exc:
                click.echo(f"  × {i.name}: failed to update {i.config_path}: {exc}", err=True)
            continue
        if yes:
            answer = "y"
        else:
            try:
                answer = input(f"Register skylakegrep with {i.name}? [Y/n] ").strip().lower()
            except EOFError:
                answer = ""
        if answer and answer not in {"y", "yes"}:
            click.echo(f"  · skipped {i.name}")
            continue
        try:
            wrote = i.register()
            if wrote:
                click.echo(f"  ✓ wrote snippet to {i.config_path}")
                n_changed += 1
            else:
                click.echo(f"  · {i.name} already had the snippet (no change)")
        except OSError as exc:
            click.echo(f"  × {i.name}: failed to write {i.config_path}: {exc}", err=True)

    integrations_mod.mark_setup_done()
    click.echo(f"\nDone. Registered/updated {n_changed} integration(s).")
    if n_changed:
        click.echo("Restart any open Claude Code / Codex / Gemini / Cursor sessions to pick up the change.")
    click.echo("Run `skygrep setup --uninstall` to remove all snippets later.")


def _collect_search_flag_names() -> list[str]:
    """Return the set of long-form flag names registered on the
    ``search`` subcommand. Used by the typo-correction wrapper in
    :func:`main` to suggest the closest match when the user types an
    unknown ``--flag``. Computed lazily on first call so importing
    this module stays cheap."""

    names: list[str] = []
    for name, command in cli.commands.items():  # type: ignore[attr-defined]
        for param in command.params:
            for opt in getattr(param, "opts", ()):
                if opt.startswith("--"):
                    names.append(opt)
    # Dedupe while preserving order.
    seen = set()
    unique = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


def main():
    """CLI entry point.

    Wraps the click invocation with a typo-correcting error handler
    (0.2.4+): when the user types an unknown flag, click's default
    is "Error: No such option: --tup" with no suggestion. We catch
    ``NoSuchOption`` and run ``difflib`` against every long-form flag
    registered on every command to surface a "Did you mean '--top'?"
    line. Falls back to click's default formatting when the typo is
    too far from any known flag (cutoff 0.6 in
    ``intelligent_cli.closest_match``).
    """

    try:
        result = cli(standalone_mode=False)
        return result if isinstance(result, int) else 0
    except click.exceptions.NoSuchOption as exc:
        suggestion = suggest_for_unknown_option(
            exc.option_name or "", _collect_search_flag_names()
        )
        if suggestion and not hints_disabled():
            click.echo(suggestion, err=True)
        else:
            exc.show()
        sys.exit(2)
    except click.exceptions.UsageError as exc:
        # Other usage errors (missing arg, conflicting flags). Click's
        # default error message is the right thing to render here; we
        # only intercept NoSuchOption above. Preserve click's exit code
        # to match standalone-mode behaviour.
        exc.show()
        sys.exit(exc.exit_code)
    except click.exceptions.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.exceptions.Abort:
        click.echo("Aborted!", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
