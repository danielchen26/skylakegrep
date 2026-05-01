import click
import json
import sqlite3
import time
from pathlib import Path
from .answerer import get_answerer
from .indexer import batch_embed, collect_indexable_files, prepare_file_chunks
from .embeddings import get_embedder
from .storage import delete_file_chunks, delete_missing_files, get_indexed_files, init_db, search, store_chunks_batch
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
        chunks = prepare_file_chunks(f)
        if chunks:
            chunks = batch_embed(chunks, embedder, batch_size=10)
            for c in chunks:
                delete_file_chunks(conn, c["file"])
            store_chunks_batch(conn, chunks)
            total_chunks += len(chunks)
            click.echo(f"  Indexed: {f} ({len(chunks)} chunks)")

    click.echo(f"Indexing complete! {total_chunks} chunks in {len(files_to_process)} files")

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
):
    cfg = get_config()
    conn = sqlite3.connect(cfg["db_path"])
    embedder = get_embedder()
    start = time.time()
    queries = [query]
    answerer = None
    if agentic:
        answerer = get_answerer()
        subqueries = answerer.decompose(query, max_queries=max_subqueries)
        for subquery in subqueries:
            if subquery not in queries:
                queries.append(subquery)
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
                        chunks = prepare_file_chunks(f)
                        if chunks:
                            chunks = batch_embed(chunks, embedder, batch_size=10)
                            for c in chunks:
                                delete_file_chunks(conn, c["file"])
                            store_chunks_batch(conn, chunks)
                            indexed_files[f_str] = current_mtime
                            click.echo(f"  Updated: {f}")
                else:
                    chunks = prepare_file_chunks(f)
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
def stats():
    cfg = get_config()
    conn = sqlite3.connect(cfg["db_path"])
    cursor = conn.execute("SELECT COUNT(*), COUNT(DISTINCT file) FROM chunks")
    row = cursor.fetchone()
    click.echo(f"Total chunks: {row[0]}")
    click.echo(f"Total files: {row[1]}")

def main():
    cli()
