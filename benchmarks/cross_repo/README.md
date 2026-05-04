# Cross-repo task sets

This directory holds **user-private** hand-curated benchmark fixtures.
Each JSON file is a list of natural-language questions paired with an
expected file or directory inside a target repository. The file is
consumed by `benchmarks/parity_vs_ripgrep.py` (and by
`benchmarks/parity_vs_mixedbread.py` when a Mixedbread account is set
up):

```bash
.venv/bin/python benchmarks/parity_vs_ripgrep.py \
  --root /path/to/your/repo \
  --tasks benchmarks/cross_repo/your-tasks.json \
  --top-k 10 --summary-only
```

The fixture files themselves are gitignored — to reproduce or extend
the published benchmarks you must build your own task set against
your own repositories. The schema below is the only stable
contract.

## Schema

```json
[
  {
    "id": "feature-001",
    "question": "Natural-language question phrased the way a user would ask it.",
    "expected": "relative/path/inside/repo",
    "expected_alternatives": [
      "optional/other/path/that/also/counts.ext"
    ],
    "ground_truth_note": "Optional free-form note explaining why this answer is canonical and when an alternative is acceptable."
  }
]
```

- `id` is opaque, used only to identify rows in the JSON report.
- `question` should be phrased in user-language (no obvious code-tokens
  that would trivially match `rg`); the harder the keyword overlap is
  to mine, the more meaningfully the comparison stresses semantic
  ranking.
- `expected` may be either an exact relative file path or a directory
  prefix; the harness uses substring matching, so a directory prefix
  counts as a hit when the search returns any chunk inside that
  directory.
- `expected_alternatives` is optional; entries are treated as "also
  acceptable" answers when the canonical `expected` is one of several
  plausible right answers.
- `ground_truth_note` is optional; ignored by the harness, useful as
  documentation for why a given answer was chosen.

## Building your own task set

1. Pick a repository with a recognizable feature decomposition.
2. Read enough of the repo to be confident which file or directory is
   the right answer for each question.
3. Write 10–30 questions; phrase them so that surface-level token
   overlap with the expected path is **weak** (e.g. ask "where is
   microphone audio captured" rather than "where is voice_input").
4. Save the JSON file here and run
   `parity_vs_ripgrep.py --tasks benchmarks/cross_repo/your-tasks.json`.

## Why these fixtures are private

Even with the repo names scrubbed from documentation, hand-labelled
benchmark fixtures inevitably contain:

- the directory structure of the target repo (file basenames, top-
  level package names);
- domain-specific feature vocabulary in the questions;
- ground-truth notes that justify why an answer is canonical.

Together these make the underlying codebase recognisable to anyone
familiar with it. Keeping the fixture files out of version control is
the simplest way to prevent that leak. The benchmark harness, the
schema, and the methodology — i.e. everything an independent reader
needs to reproduce the *protocol* — remain checked in.
