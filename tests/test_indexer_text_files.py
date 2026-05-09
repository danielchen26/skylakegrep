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


def test_collect_indexable_files_skips_hidden_and_dependency_cache(tmp_path: Path):
    visible = tmp_path / "src" / "session.py"
    visible.parent.mkdir()
    visible.write_text("def refresh_session(): pass\n")
    hidden = tmp_path / ".tool" / "session.py"
    hidden.parent.mkdir()
    hidden.write_text("def hidden(): pass\n")
    cached = tmp_path / "go" / "pkg" / "mod" / "dep" / "session.go"
    cached.parent.mkdir(parents=True)
    cached.write_text("package dep\n")

    files = collect_indexable_files(tmp_path)

    assert visible in files
    assert hidden not in files
    assert cached not in files
