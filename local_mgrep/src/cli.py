import click
import json
import sqlite3
import time
from pathlib import Path
from .answerer import get_answerer
from .indexer import batch_embed, collect_indexable_files, prepare_file_chunks
from .embeddings import get_embedder
from .storage import delete_file_chunks, delete_missing_files, get_indexed_files, init_db, populate_file_embeddings, search, store_chunks_batch
from .config import get_config


def merge_results(result_groups: list[list[dict]], top: int) -> list[dict]:
    merged = {}
    for results in result_groups:
        for result in results:
            key = (result["path"], result.get("start_line"), result.get("end_line"), result["snippet"])
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

@click.group()
def cli():
    pass

@cli.command()
@click.argument("path", default=".")
@click.option("--reset", is_flag=True, help="Reset existing index")
@click.option("--incremental/--full", default=True, help="Only update changed files")
def index(path: str, reset: bool, incremental: bool):
    cfg = get_config()
    db_path = cfg["db_path"]
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
        click.echo(f"Incremental: {len(to_index)} new, {len(to_reindex)} changed, {len(deleted_files)} deleted")
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

    click.echo(f"Indexing complete! {total_chunks} chunks in {len(files_to_process)} files")
    file_count = populate_file_embeddings(conn)
    click.echo(f"File-level embeddings populated: {file_count} files (mean of chunk vectors)")

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
@click.option("--rerank/--no-rerank", default=True, help="Apply cross-encoder reranking (requires sentence-transformers; install with pip install 'local-mgrep[rerank]')")
@click.option("--rerank-pool", default=None, type=int, help="Candidate pool size before reranking (default 50, env MGREP_RERANK_POOL)")
@click.option("--rerank-model", default=None, help="HuggingFace cross-encoder model id for reranking")
@click.option("--hyde/--no-hyde", default=False, help="Use the local LLM to generate a hypothetical code answer for natural-language queries, then embed both the question and that doc (slower; helps recall on user-language queries)")
@click.option("--multi-resolution/--no-multi-resolution", default=True, help="Two-stage retrieval: pick top-N files by file-level cosine first, then drill into their chunks (helps small canonical files compete against large consumer files)")
@click.option("--file-top", default=30, type=int, help="Number of files surfaced by file-level retrieval before chunk-level scoring (only used with --multi-resolution)")
@click.option("--lexical-prefilter/--no-lexical-prefilter", default=True, help="Use ripgrep to narrow the candidate file set before cosine + rerank (default on; this is the high-recall fast path)")
@click.option("--lexical-root", default=None, help="Root directory ripgrep scans for the lexical prefilter (defaults to the working directory)")
@click.option("--lexical-min-candidates", default=2, type=int, help="If ripgrep returns fewer than this many candidate files we fall back to corpus-wide cosine retrieval")
@click.option("--daemon-url", default=None, help="If set, send the search to a running mgrep daemon instead of loading the reranker in-process (eliminates cold-load latency)")
@click.option("--rank-by", default="chunk", type=click.Choice(["chunk", "file"]), help="Ranking strategy: 'chunk' (default) returns top-K chunks with per-file diversity cap; 'file' returns one best chunk per file, sorted by that score")
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
):
    cfg = get_config()
    if daemon_url:
        from .server import daemon_search

        start = time.time()
        try:
            payload = daemon_search(
                daemon_url,
                query,
                top_k=top,
                rerank=rerank,
                rerank_pool=rerank_pool if rerank_pool is not None else cfg["rerank_pool"],
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
            click.echo(f"\n[Daemon search completed in {elapsed:.3f}s; daemon-side {payload.get('latency_seconds')}s]")
            return
    conn = sqlite3.connect(cfg["db_path"])
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
    pool = rerank_pool if rerank_pool is not None else cfg["rerank_pool"]
    if hyde:
        if answerer is None:
            answerer = get_answerer()
        queries = [answerer.hyde(item) for item in queries]
    candidate_paths = None
    if lexical_prefilter:
        from pathlib import Path as _Path

        from .hybrid import lexical_candidate_paths

        prefilter_root = _Path(lexical_root) if lexical_root else _Path.cwd()
        cands = lexical_candidate_paths(query, prefilter_root)
        if len(cands) >= lexical_min_candidates:
            candidate_paths = cands
        # else fall through to corpus-wide cosine
    result_groups = []
    for item in queries:
        query_embedding = embedder.embed(item)
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
            click.echo(f"- {result['path']}{line_range} (score: {result['score']:.3f})")
        click.echo(f"\n[Answer completed in {elapsed:.3f}s]")
        return
    for r in results:
        line_range = ""
        if r.get("start_line") and r.get("end_line"):
            line_range = f":{r['start_line']}-{r['end_line']}"
        click.echo(f"\n=== {r['path']}{line_range} (score: {r['score']:.3f}) ===")
        if content:
            click.echo(r["snippet"][:500])
    click.echo(f"\n[Search completed in {elapsed:.3f}s]")

@cli.command()
@click.argument("path", default=".")
@click.option("--interval", "-i", default=5, help="Check interval in seconds")
def watch(path: str, interval: int):
    cfg = get_config()
    db_path = cfg["db_path"]
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
    """Run a long-running daemon that holds the reranker + embedder warm.

    A short-lived ``mgrep search`` invocation pays the cross-encoder cold
    load on every call (~30 s for the large reranker on Mac CPU). This
    daemon eliminates that cost: start it once, point ``mgrep search
    --daemon-url http://127.0.0.1:7878`` at it, and per-query latency
    drops to inference-only (~1-3 s on this hardware).
    """

    from .server import serve as _serve

    _serve(host=host, port=port)


@cli.command()
def stats():
    cfg = get_config()
    conn = sqlite3.connect(cfg["db_path"])
    cursor = conn.execute("SELECT COUNT(*), COUNT(DISTINCT file) FROM chunks")
    row = cursor.fetchone()
    click.echo(f"Total chunks: {row[0]}")
    click.echo(f"Total files: {row[1]}")

def main():
    cli()


if __name__ == "__main__":
    main()
