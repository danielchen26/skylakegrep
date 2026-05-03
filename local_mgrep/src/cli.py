"""Command-line entry point for ``mgrep``.

Two-mode CLI:

  - **Bare-form** (95% of use): ``mgrep "<query>"`` runs a search with smart
    defaults. The first argument is treated as a query whenever it isn't a
    known subcommand. Auto-index runs the first time a project is queried,
    incremental refresh runs on subsequent queries when files have changed.
  - **Subcommand form** (admin / power use): ``mgrep <verb> [args]`` for
    ``index``, ``watch``, ``serve``, ``stats``, ``doctor``, and the explicit
    ``search`` form. ``mgrep --help`` prints the full surface.

Routing rule: known subcommands win. Anything that isn't a known subcommand
is treated as the first argument to ``search``. A query that happens to
collide with a subcommand name (rare) can be quoted: ``mgrep "stats and
metrics"`` searches; ``mgrep stats`` runs the subcommand.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import click

logger = logging.getLogger(__name__)

from . import auto_index, bootstrap, code_graph, config as cfg_mod, enrich as enrich_mod
from .answerer import get_answerer
from .config import get_config
from .embeddings import get_embedder
from .indexer import batch_embed, collect_indexable_files, prepare_file_chunks
from .storage import (
    CASCADE_DEFAULT_TAU,
    cascade_search,
    delete_file_chunks,
    delete_missing_files,
    get_indexed_files,
    init_db,
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


def render_json_results(results: list[dict]) -> str:
    payload = [
        {
            "path": r["path"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
            "language": r["language"],
            "score": float(r["score"]),
            "snippet": r["snippet"],
        }
        for r in results
    ]
    return json.dumps(payload, indent=2)


# Subcommand names that take precedence over bare-form query routing.
_SUBCOMMANDS = {"index", "search", "watch", "serve", "stats", "doctor", "enrich"}


class MgrepCLI(click.Group):
    """Click group that routes unknown first-args to ``search``.

    Implements two adjustments to default Click behaviour:
      - ``mgrep "<query>"`` (no subcommand) routes to ``search "<query>"``.
      - ``mgrep stats and metrics`` (subcommand-shaped but with extra args
        that don't fit) prints a friendly suggestion to quote the query.
    """

    def parse_args(self, ctx, args):  # type: ignore[override]
        # If first non-flag token is not a known subcommand, treat the whole
        # arg list as the search query.
        if args and not args[0].startswith("-") and args[0] not in _SUBCOMMANDS:
            return super().parse_args(ctx, ["search", *args])
        return super().parse_args(ctx, args)


@click.group(cls=MgrepCLI, invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """``mgrep`` — local semantic code search.

    Common usage:

        mgrep "where is the auth token refreshed?"      # bare query
        mgrep doctor                                    # health check
        mgrep stats                                     # index info
        mgrep index .                                   # explicit reindex
    """

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
@click.argument("path", default=".")
@click.option("--reset", is_flag=True, help="Reset existing index before reindexing")
@click.option("--incremental/--full", default=True, help="Only update changed files")
def index(path: str, reset: bool, incremental: bool):
    """Build or refresh the index explicitly. ``mgrep search`` already
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
    for f in files_to_process:
        chunks = prepare_file_chunks(f, root=root)
        if chunks:
            chunks = batch_embed(chunks, embedder, batch_size=10)
            for c in chunks:
                delete_file_chunks(conn, c["file"])
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


@cli.command()
@click.argument("query")
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
@click.option("--rerank-pool", default=None, type=int, help="Candidate pool size before reranking (default 50, env MGREP_RERANK_POOL)")
@click.option("--rerank-model", default=None, help="HuggingFace cross-encoder model id for reranking")
@click.option("--hyde/--no-hyde", default=False, help="Force a HyDE rewrite even outside the cascade (rarely needed; the cascade decides per query)")
@click.option("--multi-resolution/--no-multi-resolution", default=True, help="Two-stage retrieval: pick top-N files by file-level cosine first, then drill into their chunks (helps small canonical files compete against large consumer files)")
@click.option("--file-top", default=30, type=int, help="Number of files surfaced by file-level retrieval before chunk-level scoring (only used with --multi-resolution)")
@click.option("--lexical-prefilter/--no-lexical-prefilter", default=True, help="Use ripgrep to narrow the candidate file set before cosine + rerank (default on; the high-recall fast path)")
@click.option("--lexical-root", default=None, help="Root directory ripgrep scans for the lexical prefilter (defaults to the project root)")
@click.option("--lexical-min-candidates", default=2, type=int, help="If ripgrep returns fewer than this many candidate files we fall back to corpus-wide cosine retrieval")
@click.option("--daemon-url", default=None, help="If set, send the search to a running mgrep daemon instead of loading the reranker in-process (eliminates cold-load latency)")
@click.option("--rank-by", default="chunk", type=click.Choice(["chunk", "file"]), help="Ranking strategy on the non-cascade path: 'chunk' returns top-K chunks with per-file diversity cap; 'file' returns one best chunk per file")
@click.option("--cascade/--no-cascade", default=True, help="Confidence-gated retrieval (default on): cheap file-mean cosine first, escalate to HyDE-union only on uncertain queries. Pass --no-cascade for the chunk-only legacy path.")
@click.option("--cascade-tau", default=CASCADE_DEFAULT_TAU, type=float, help=f"Confidence threshold (top1 - top2 file-mean cosine) above which the cascade returns the cheap result. Default {CASCADE_DEFAULT_TAU}.")
@click.option("--auto-index/--no-auto-index", default=None, help="Auto-build the index for this project on first query and refresh on subsequent queries. Default: on for the project-scoped DB; off when MGREP_DB_PATH is set externally so curated indexes are not auto-mutated.")
def search_cmd(
    query: str,
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
    daemon_url: str,
    rank_by: str,
    cascade: bool,
    cascade_tau: float,
    auto_index: bool | None,
):
    """Run a search. Aliased as the bare form: ``mgrep "<query>"``."""

    import os as _os

    config = get_config()
    if auto_index is None:
        # Default policy: auto-index unless the caller has pinned the DB
        # location with MGREP_DB_PATH (curated index — don't auto-mutate it).
        auto_index = _os.environ.get("MGREP_DB_PATH") is None
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
            if json_output:
                click.echo(render_json_results(results))
                return
            for r in results:
                line_range = ""
                if r.get("start_line") and r.get("end_line"):
                    line_range = f":{r['start_line']}-{r['end_line']}"
                click.echo(f"\n=== {r['path']}{line_range} (score: {r['score']:.3f}) ===")
                if content:
                    click.echo(r["snippet"][:500])
            click.echo(
                f"\n[Daemon search completed in {elapsed:.3f}s; "
                f"daemon-side {payload.get('latency_seconds')}s]"
            )
            return

    project_root = cfg_mod.project_root()
    db_path = config["db_path"]

    from . import auto_index as ai

    # Routing decision: ready → cascade; building or absent → rg fallback.
    conn = init_db(db_path)
    ready = ai.is_index_ready(conn)
    if not ready and auto_index:
        # Spawn (or no-op if already running) a detached indexer; do NOT
        # block the user. The next query that lands after the spawn
        # finishes will get full semantic results.
        try:
            ai.spawn_background_index(project_root, db_path)
        except Exception as exc:
            logger.warning("background index spawn failed: %s", exc)
        # Fall back to a pure-rg result for this query.
        start = time.time()
        results = ai.rg_fallback_results(query, project_root, top_k=top)
        elapsed = time.time() - start
        if json_output:
            click.echo(render_json_results(results))
            return
        if not results:
            click.echo(
                "No matches yet. Semantic index is building in the background; "
                "try the same query again in a minute, or run `mgrep stats` to "
                "see progress.",
                err=True,
            )
            return
        for r in results:
            line_range = ""
            if r.get("start_line") and r.get("end_line"):
                line_range = f":{r['start_line']}-{r['end_line']}"
            click.echo(f"\n=== {r['path']}{line_range} (score: {r['score']:.3f}) ===")
            if content:
                click.echo(r["snippet"][:500])
        building = ai.is_index_building(db_path)
        suffix = "building in background" if building else "queued"
        click.echo(
            f"\n[{elapsed:.3f}s · ripgrep fallback · semantic index {suffix}]"
        )
        return

    # Index is ready (or auto_index disabled): run the normal pipeline,
    # plus an mtime-based incremental refresh on the way in.
    if auto_index:
        try:
            ai.incremental_refresh(
                conn,
                project_root,
                throttle_seconds=ai._refresh_throttle_from_env(),
                quiet=json_output,
            )
        except Exception as exc:
            logger.warning("auto-refresh failed: %s", exc)

    # L2 one-time symbol extraction. The ``symbols`` table is created by
    # ``init_db`` but is empty on indexes built before L2 — populate it on
    # first use, best-effort so a parser failure or filesystem issue can't
    # block the search itself. Stay quiet on the JSON path so stable
    # consumers (CliRunner, scripts) keep parsing the output cleanly.
    if not _symbols_table_populated(conn):
        try:
            if not json_output:
                click.echo("↻ extracting symbols (one-time, no LLM)…", err=True)
            inserted = populate_symbols(conn, project_root)
            if not json_output:
                click.echo(f"✓ {inserted} symbols indexed", err=True)
        except Exception as exc:
            logger.warning("symbol indexing failed: %s", exc)

    # L4 one-time migration: build the file-export graph if the table is
    # empty. Best-effort; failures here must not block search. Suppressed
    # under ``--json`` so machine-readable callers see clean stdout.
    graph_ready = False
    try:
        row = conn.execute("SELECT COUNT(*) FROM file_graph").fetchone()
        graph_count = row[0] if row else 0
        if graph_count == 0:
            if not json_output:
                click.echo("↻ building file-export graph (one-time)…", err=True)
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

    embedder = get_embedder(role="query")
    start = time.time()
    queries = [query]
    answerer = None
    if agentic:
        answerer = get_answerer()
        subqueries = answerer.decompose(query, max_queries=max_subqueries)
        for subquery in subqueries:
            if subquery not in queries:
                queries.append(subquery)
    pool = rerank_pool if rerank_pool is not None else config["rerank_pool"]
    if hyde and not cascade:
        if answerer is None:
            answerer = get_answerer()
        queries = [answerer.hyde(item) for item in queries]
    candidate_paths = None
    if lexical_prefilter:
        from .hybrid import lexical_candidate_paths

        prefilter_root = Path(lexical_root) if lexical_root else project_root
        cands = lexical_candidate_paths(query, prefilter_root)
        if len(cands) >= lexical_min_candidates:
            candidate_paths = cands
        # else fall through to corpus-wide cosine

    result_groups: list[list[dict]] = []
    cascade_telemetry: dict | None = None
    for item in queries:
        query_embedding = embedder.embed(item)
        if cascade:
            if answerer is None:
                answerer = get_answerer()
            cascade_results, cascade_telemetry = cascade_search(
                conn,
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
    results = merge_results(result_groups, top)
    elapsed = time.time() - start
    if json_output:
        click.echo(render_json_results(results))
        return
    if answer:
        if answerer is None:
            answerer = get_answerer()
        synthesized = answerer.answer(query, results)
        click.echo(synthesized)
        click.echo("\nSources:")
        for result in results:
            line_range = ""
            if result.get("start_line") and result.get("end_line"):
                line_range = f":{result['start_line']}-{result['end_line']}"
            click.echo(
                f"- {result['path']}{line_range} (score: {result['score']:.3f})"
            )
        click.echo(f"\n[Answer completed in {elapsed:.3f}s]")
        return
    for r in results:
        line_range = ""
        if r.get("start_line") and r.get("end_line"):
            line_range = f":{r['start_line']}-{r['end_line']}"
        click.echo(f"\n=== {r['path']}{line_range} (score: {r['score']:.3f}) ===")
        if content:
            click.echo(r["snippet"][:500])

    parts: list[str] = [f"{elapsed:.3f}s"]
    if cascade and cascade_telemetry is not None:
        kind = "cheap" if cascade_telemetry.get("early_exit") else "escalated"
        parts.append(
            f"cascade={kind} (gap={cascade_telemetry.get('gap', 0):.4f} "
            f"τ={cascade_telemetry.get('tau', 0):.4f})"
        )
    parts.append(f"index {ai.index_age_human(conn)} · {status['files']} files")
    if _symbols_table_populated(conn):
        parts.append("L2 symbols on")
    if graph_ready:
        parts.append("graph prior on")
        if any(r.get("graph_tiebreak") for r in results):
            # Surface the tied gap (top1-top2 of the pre-tiebreak rankings is
            # not retained, so report the post-tiebreak top1-top2 as a proxy).
            if len(results) >= 2:
                gap = float(results[0].get("score", 0)) - float(results[1].get("score", 0))
                parts.append(f"tied (Δ={gap:.3f})")
            else:
                parts.append("tied")
    click.echo("\n[" + " · ".join(parts) + "]")

    # Optional background auto-enrich. Off by default; users opt in by
    # exporting ``MGREP_AUTO_ENRICH=yes``. We only spawn when the index is
    # actually ready — never alongside the rg-fallback path — so we don't
    # contend with the still-running first-time indexer for Ollama.
    if (
        ready
        and _os.environ.get("MGREP_AUTO_ENRICH") == "yes"
        and enrich_mod.count_pending(conn) > 0
    ):
        try:
            import subprocess as _subprocess

            log = db_path.with_suffix(db_path.suffix + ".enrich.log")
            env = dict(_os.environ)
            env["MGREP_DB_PATH"] = str(db_path)
            _subprocess.Popen(
                [sys.executable, "-m", "local_mgrep.src.cli", "enrich", "--max", "50"],
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
            for f in files:
                f_str = str(f)
                current_mtime = f.stat().st_mtime
                if f_str in indexed_files:
                    if current_mtime > indexed_files[f_str]:
                        chunks = prepare_file_chunks(f, root=root)
                        if chunks:
                            chunks = batch_embed(chunks, embedder, batch_size=10)
                            for c in chunks:
                                delete_file_chunks(conn, c["file"])
                            store_chunks_batch(conn, chunks)
                            indexed_files[f_str] = current_mtime
                            click.echo(f"  Updated: {f}")
                else:
                    chunks = prepare_file_chunks(f, root=root)
                    if chunks:
                        chunks = batch_embed(chunks, embedder, batch_size=10)
                        store_chunks_batch(conn, chunks)
                        indexed_files[f_str] = current_mtime
                        click.echo(f"  Added: {f}")
            time.sleep(interval)
        except KeyboardInterrupt:
            click.echo("\nStopping watch mode")
            break


@cli.command()
@click.option("--host", default="127.0.0.1", help="Host to bind the daemon on")
@click.option("--port", default=7878, type=int, help="Port to bind the daemon on")
def serve(host: str, port: int):
    """Run a long-running daemon that holds the reranker + embedder warm."""

    from .server import serve as _serve

    _serve(host=host, port=port)


@cli.command()
def stats():
    """Print chunk and file counts for the current project's index."""

    config = get_config()
    db_path = config["db_path"]
    click.echo(f"DB:           {db_path}")
    click.echo(f"Project root: {cfg_mod.project_root()}")
    if not db_path.exists():
        click.echo("No index yet. Run a query to auto-index, or `mgrep index .`.")
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
            "No index yet. Run `mgrep index .` (or just query the project) "
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

    config = get_config()
    report = bootstrap.doctor_report(config["ollama_url"])
    pad = lambda label: f"  {label:<26}"  # noqa: E731
    click.echo("mgrep doctor")
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
        click.echo(f"{pad('Project index')}× not yet built — run a query to auto-index, or `mgrep index .`")
        click.echo(f"{pad('Would write to')}{db_path}")
    # Reranker presence is a soft check.
    try:
        import sentence_transformers  # noqa: F401

        click.echo(f"{pad('Reranker (optional)')}✓ sentence-transformers installed")
    except ImportError:
        click.echo(f"{pad('Reranker (optional)')}— install: pip install 'local-mgrep[rerank]'")
    click.echo(f"{pad('Project root')}{cfg_mod.project_root()}")


def main():
    cli()


if __name__ == "__main__":
    main()
