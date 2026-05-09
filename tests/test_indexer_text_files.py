from __future__ import annotations

from pathlib import Path

from skylakegrep.src.indexer import collect_indexable_files, prepare_file_chunks


def test_markdown_files_are_indexable_content(tmp_path: Path):
    note = tmp_path / "notes" / "rate_limits.md"
    note.parent.mkdir()
    note.write_text(
        "# Rate Limits\n"
        "The retry subsystem slows repeated calls so external APIs are not hammered.\n"
    )

    files = collect_indexable_files(tmp_path)
    chunks = prepare_file_chunks(note, root=tmp_path)

    assert note in files
    assert chunks
    assert chunks[0]["language"] == "markdown"
    assert "external APIs" in chunks[0]["chunk"]
