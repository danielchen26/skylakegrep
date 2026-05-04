# local-mgrep 0.4.1 — release notes

A targeted UX patch on top of 0.4.0. The first query against a fresh
project no longer blocks the user while the semantic index builds — it
returns ripgrep-level results in well under a second, and the index
builds in a detached background process while you keep working.

## The problem 0.4.1 fixes

In 0.4.0 the bare-form ``mgrep "<query>"`` would auto-index a fresh
project on the user's first query. The auto-index is correct — but it
runs synchronously, and on a 200-file project that's 60 to 90 seconds of
nothing on screen. A user who Ctrl+Cs out (because the prompt looked
hung) ended up with a partially-populated index and no way to recover
without ``mgrep index . --reset``. That is not a productive experience.

## What changed

### Ripgrep fallback for the first query

When ``mgrep "<query>"`` runs against a project whose semantic index
isn't ready yet, the CLI:

  1. Spawns a detached background indexer (``mgrep index <root>`` in a
     new session, PID written to ``<db>.lock``, log to ``<db>.log``).
  2. Runs ripgrep on the extracted query terms against the project root.
  3. Returns the top files, each with a 24-line head snippet and a
     simple recall-based score (number of distinct query terms hit).
  4. Prints a status line that tells the user exactly what just
     happened.

```
$ time mgrep "where is the <domain-y> neural network architecture defined?" --top 3 --no-content

=== /path/to/repo-B/docs/companion_notes/repo-B_companion_note.tex:14-37 (score: 0.800) ===
=== /path/to/repo-B/docs/plans/2026-04-20-<draft-z>.md:138-161 (score: 0.800) ===
=== /path/to/repo-B/.../<dataset-y>.json:760-783 (score: 0.800) ===

[0.672s · ripgrep fallback · semantic index building in background]
real    0m1.505s
```

Once the background indexer finishes (typically 1–3 minutes for a
medium project), the next query you run in the same project gets the
full semantic cascade instead.

### Readiness predicate

A new ``auto_index.is_index_ready(conn)`` returns ``True`` only when
``meta.last_full_index_at`` is set — which happens at the very end of
``first_time_index`` after ``populate_file_embeddings`` succeeds. This
correctly identifies partial / interrupted index states (chunks present
but file-embeddings missing) as **not ready** and re-runs the
background indexer instead of using a corrupt index.

### Detached background indexer

``auto_index.spawn_background_index`` launches the indexer with
``start_new_session=True`` so the user's Ctrl+C, terminal close, or
shell exit does not kill it. The PID is written to ``<db>.lock``;
crashed indexers leave stale lockfiles that ``is_index_building``
detects and removes on the next query.

### Query results when no rg matches

If the user's query has no surface-level overlap with any indexed file
(rare for natural-language queries but possible), the fallback returns
no results and prints:

```
No matches yet. Semantic index is building in the background; try the
same query again in a minute, or run `mgrep stats` to see progress.
```

### ``mgrep stats`` shows live progress

``mgrep stats`` (run from inside the project) shows the indexer's
running tally even while the indexer is mid-run:

```
DB:           /Users/.../local-mgrep/repos/repo-B-21aa34e5.db
Project root: /Users/.../repo-B
Total chunks: 198
Total files:  6
```

The numbers update with each batch the background indexer commits.

## CLI surface

No flags added or removed. The flow is fully backwards compatible.

  - ``--no-auto-index`` still disables the whole machinery.
  - ``MGREP_DB_PATH`` still flips auto-index off (curated indexes are
    not auto-mutated).
  - ``mgrep index .`` is still the explicit / synchronous index path
    when you want to wait for completion.

## What changed under the hood

  - ``local_mgrep/src/auto_index.py`` — added ``is_index_ready``,
    ``is_index_building``, ``spawn_background_index``, and
    ``rg_fallback_results``; reused the existing
    ``hybrid.extract_query_terms`` so rg-fallback term parsing matches
    the prefilter that would run later.
  - ``local_mgrep/src/cli.py`` — ``search_cmd`` now branches on
    ``is_index_ready``: on ready, run the cascade as in 0.4.0; on
    not-ready, fall back to ripgrep + spawn background indexer.

## Compatibility

  - 24 / 24 unit tests still pass.
  - All 0.4.0 flags continue to work as documented.
  - The cascade default introduced in 0.4.0 is unchanged.
  - Existing project indexes (built under 0.4.0) are picked up and used
    as-is by 0.4.1; no reindex required.

## Install

```
pip install --upgrade local-mgrep
```
