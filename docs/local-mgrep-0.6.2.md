# local-mgrep 0.6.2 — release notes

Polish release. No new retrieval architecture, no recall change. Three
quality-of-life improvements:

  - **Ollama preheat on every search.** ``mgrep`` now fires fire-and-
    forget background threads at the start of every search to load the
    embed model and the HyDE model into Ollama with ``keep_alive=-1``.
    The first cascade-escalation in a fresh shell session no longer
    pays the full 5-10 s per-model cold-load — Ollama loads in
    parallel with rg prefilter, file-mean cosine, and DB migrations,
    so the actual search blocks only on inference time.
  - **GitHub Actions CI.** Two new workflows:
    [`.github/workflows/test.yml`](../.github/workflows/test.yml) runs
    pytest on every push and PR across Python 3.10 / 3.11 / 3.12;
    [`.github/workflows/release.yml`](../.github/workflows/release.yml)
    builds and publishes to PyPI on every ``vX.Y.Z`` tag push, gated
    on a ``PYPI_API_TOKEN`` repository secret. The pre-existing pages
    workflow that publishes ``docs/`` to GitHub Pages is unchanged.
  - **Social preview card.**
    [`docs/assets/og-image.png`](assets/og-image.png) (1200 × 630)
    now backs the GitHub repo social preview and the `og:image` meta
    in [`docs/index.html`](index.html) for any social-card-fetching
    consumer (Twitter, LinkedIn, Slack, …). Source SVG at
    [`docs/assets/og-image.svg`](assets/og-image.svg).

## What changed

  - ``local_mgrep/src/bootstrap.py`` — new ``preheat_models()``
    helper that fires two daemon threads (one per model) at HTTP
    POST against ``/api/embeddings`` and ``/api/generate`` with
    ``keep_alive=-1`` and ``num_predict=1``. Failures are silently
    swallowed.
  - ``local_mgrep/src/cli.py`` — ``search_cmd`` calls
    ``bootstrap.preheat_models()`` immediately after the daemon-url
    branch, before any DB / index work.
  - ``.github/workflows/test.yml`` — pytest matrix CI.
  - ``.github/workflows/release.yml`` — auto-publish on tag.
  - ``docs/assets/og-image.svg`` + ``og-image.png`` — social card.
  - ``docs/index.html`` — ``og:`` and ``twitter:`` meta tags wired
    to ``og-image.png``; brand version label bumped to ``v0.6.2``.
  - ``docs/assets/hero-dark.svg`` — version bump v0.6.1 → v0.6.2.
  - ``pyproject.toml`` — version bump.

## Compatibility

  - 40 / 40 unit tests pass.
  - All 0.5.x / 0.6.x flags remain valid.
  - Recall on repo-A 16-task with default config: **16 / 16**.
  - Preheat is best-effort and silently swallows network / DNS /
    timeout failures — disabling Ollama still produces the same
    error-handling path on the real search call as before.

## Install

```
pip install --upgrade local-mgrep
```
