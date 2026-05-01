import click
import sqlite3
import time
from pathlib import Path
from .indexer import prepare_file_chunks, batch_embed, SUPPORTED_EXTENSIONS
from .embeddings import get_embedder
from .storage import init_db, store_chunks_batch, search, delete_file_chunks, get_indexed_files, get_file_mtime
from .config import get_config

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

    files = []
    for ext in SUPPORTED_EXTENSIONS:
        files.extend(Path(path).rglob(f"*{ext}"))

    click.echo(f"Found {len(files)} files to index")

    if incremental and not reset:
        indexed_files = get_indexed_files(conn)
        to_index = []
        to_reindex = []
        for f in files:
            f_str = str(f)
            if f_str not in indexed_files:
                to_index.append(f)
            elif f.stat().st_mtime > indexed_files[f_str]:
                to_reindex.append(f)
        click.echo(f"Incremental: {len(to_index)} new, {len(to_reindex)} changed")
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
@click.option("--top", "-n", default=5, help="Number of results")
def search_cmd(query: str, top: int):
    cfg = get_config()
    conn = sqlite3.connect(cfg["db_path"])
    embedder = get_embedder()
    start = time.time()
    query_embedding = embedder.embed(query)
    results = search(conn, query_embedding, top)
    elapsed = time.time() - start
    for r in results:
        click.echo(f"\n=== {r['file']} (score: {r['score']:.3f}) ===")
        click.echo(r["chunk"][:500])
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
            files = []
            for ext in SUPPORTED_EXTENSIONS:
                files.extend(Path(path).rglob(f"*{ext}"))

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