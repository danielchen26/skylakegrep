# Cross-repo task sets

Each JSON file in this directory is a hand-curated list of natural-language
questions and an expected file or directory inside a target repository. The
file is consumed by `benchmarks/parity_vs_ripgrep.py` (and by
`benchmarks/parity_vs_mixedbread.py` when a Mixedbread account is set up):

```bash
.venv/bin/python benchmarks/parity_vs_ripgrep.py \
  --root /path/to/repo \
  --tasks benchmarks/cross_repo/<repo>.json \
  --top-k 10 --summary-only
```

## Schema

```json
[
  {
    "id": "feature-001",
    "question": "Natural-language question phrased the way a user would ask it.",
    "expected": "relative/path/inside/repo"
  }
]
```

- `id` is opaque, used only to identify rows in the JSON report.
- `question` should be phrased in user-language (no obvious code-tokens
  that would trivially match `rg`); the harder the keyword overlap is to
  mine, the more meaningfully the comparison stresses semantic ranking.
- `expected` may be either an exact relative file path
  (e.g. `app/src/command_palette.rs`) or a directory prefix
  (e.g. `crates/ai/`); the harness uses substring matching, so a directory
  prefix counts as a hit when the search returns any chunk inside that
  directory.

## Existing task sets

| File | Target repository | Tasks | Notes |
| --- | --- | :---: | --- |
| `repo-a.json` | the [Repo-A terminal](the Rust terminal source tree (URL redacted)) Rust workspace | 16 | Mix of `crates/` features (AI, computer-use, editor, LSP, vim, voice, completer, fuzzy match, settings, websocket, secrets, markdown) and `app/src/` features (command palette, auth, billing, code review). |

## Adding a new task set

1. Pick a repository with a recognizable feature decomposition (crate
   names, directory naming, or top-level modules that imply purpose).
2. Read enough of the repo to be confident which file or directory is the
   right answer for each question.
3. Write 10–30 questions; phrase them so that surface-level token overlap
   with the expected path is **weak** (e.g. ask "where is microphone audio
   captured" rather than "where is voice_input").
4. Save the JSON file here and run
   `parity_vs_ripgrep.py --tasks benchmarks/cross_repo/<your>.json`.
