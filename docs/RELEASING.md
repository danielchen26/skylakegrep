# Release checklist for skylakegrep

This checklist exists because the 0.2.2 release shipped to PyPI but
its features (intelligent recovery, routing transparency) never
made it onto the GitHub Pages site or the README — a new user
landing on either surface had no way to find out the features
existed. From 0.2.3 onwards, no release leaves the workstation
without the full surface sync.

A release is **not** a `git tag` + `twine upload`. It is a
coordinated update across **eight surfaces**, every one of which
has to be touched (or explicitly skipped with a documented
reason).

## Eight surfaces, every release

  1. **`pyproject.toml`** — bump `version`. Required for the CI
     `Verify the tag matches pyproject.toml` step to pass.
  2. **`docs/skylakegrep-X.Y.Z.md`** — new release-notes file.
     Use the previous release's file as a template: headline,
     change-by-change bullets with the **why** (not just the what),
     compatibility notes, bench numbers (or "unchanged"), known
     follow-ups list.
  3. **`README.md`**:
       - "What's new in 0.2.x" table — add one row per
         user-visible change in this release.
       - Capability matrix at the bottom — add one row per
         capability with the version it was introduced in.
       - Any stale defaults (e.g. `ollama pull <old-model>`,
         `OLLAMA_EMBED_MODEL` default) swept.
       - Footer link list to release notes — add the new version.
  4. **`docs/index.html`** (the GitHub Pages site):
       - Hero `<p class="eyebrow">vX.Y.Z · …</p>` version bumped.
       - Hero `<h1>` headline — bumped if the headline number
         changed.
       - "Why skylakegrep?" comparison table — add or update
         a row if this release changed the answer to a row.
       - "How it works" three-step diagram — update step
         labels (model name + latency) if that changed.
       - "Benchmarks summary" three big numbers — bump if a
         headline number changed.
       - "Honesty" limitations list — extend if scope changed.
       - `og:title` / `og:description` / `twitter:description`
         meta tags — bump if the headline number changed.
  4b. **Subpages** (added in 0.2.14 — `docs/concepts.html`,
      `docs/architecture.html`, `docs/cli.html`,
      `docs/reference.html`, `docs/benchmarks.html`,
      `docs/changelog.html`):
       - `docs/cli.html` — add any new command or changed flag
         default to the cheatsheet + env-var table.
       - `docs/architecture.html` — extend the schema diagram if
         a new index column or metadata field was added.
       - `docs/benchmarks.html` — add a row to the relevant
         sub-benchmark table; rerun the worked example if its
         numbers shifted.
       - `docs/reference.html` — extend the JSON schema if a new
         field was added to the agent contract.
       - `docs/changelog.html` — add a new release-card with a
         link to `docs/skylakegrep-X.Y.Z.md`.
       - `docs/concepts.html` — extend if the indexing /
         ranking / output mental model changed.
  5. **GitHub repo description** (`gh repo edit --description …`)
     — only if the project's one-liner positioning changed.
  6. **PyPI upload**: `python -m build` then
     `twine upload dist/skylakegrep-X.Y.Z*`. The CI workflow tries
     this on tag push but currently 403s on the `PYPI_API_TOKEN`
     secret; the manual `~/.pypirc` flow is the working path until
     that's fixed.
  7. **GitHub Release**: `gh release create vX.Y.Z --target master
     --title "vX.Y.Z — …" --notes-file docs/skylakegrep-X.Y.Z.md
     dist/skylakegrep-X.Y.Z-py3-none-any.whl
     dist/skylakegrep-X.Y.Z.tar.gz`. The artifacts must be
     attached so the Release page is a self-contained record.
  8. **`git tag -a vX.Y.Z`** + `git push origin vX.Y.Z`. Tag
     **after** the version bump is committed so CI's tag-vs-version
     check doesn't fail.

## Per-step ordering

Order matters. The order below is the one that prevents partial
state from being visible to anyone:

```
1.  bump pyproject + write release notes + sweep README +
    sweep index.html + (optionally) repo description
2.  pytest -q tests/                        ← 134/134 must pass
3.  git add -p && git commit -m "release: vX.Y.Z — …"
4.  git push origin master                  ← surfaces are live
                                              with the new content
                                              before the tag
                                              announces a release
5.  python -m build                         ← produces dist/*
6.  twine check dist/*                      ← both PASSED
7.  git tag -a vX.Y.Z -m "vX.Y.Z — …"
8.  git push origin vX.Y.Z                  ← CI fires; ignore the
                                              PyPI-401 step until
                                              the token gets fixed
9.  twine upload --non-interactive dist/skylakegrep-X.Y.Z*
                                            ← manual upload
                                              (CI replacement)
10. gh release create vX.Y.Z --target master
        --title "vX.Y.Z — …"
        --notes-file docs/skylakegrep-X.Y.Z.md
        dist/skylakegrep-X.Y.Z-py3-none-any.whl
        dist/skylakegrep-X.Y.Z.tar.gz
11. verify:
        curl -s https://pypi.org/pypi/skylakegrep/json | jq .info.version
        gh release list --limit 3
```

## What "release" does NOT mean

  - **A code-only commit is not a release.** Code reaches users
    only when the eight surfaces above are sync'd. If you don't
    want to ship to PyPI, name your work as a milestone commit,
    not a release.
  - **A docs-only commit between releases is fine.** Major doc
    overhauls between releases are normal; they don't require a
    version bump unless they change the public surface
    (e.g. updating the headline benchmark number on the GitHub
    Pages site is enough to warrant a metadata bump, à la 0.2.1
    → 0.2.3).
  - **Skipping any of the eight surfaces requires a documented
    reason** in the release-notes "Known follow-ups" section.
    "I forgot" is not a documented reason.

## When to bump major / minor / patch

skylakegrep follows SemVer 2.0 with these conventions:

  - **Major (1.x → 2.x)** — reserved for the post-1.0 era; not
    relevant yet.
  - **Minor (0.x → 0.y)** — public-API additions, behaviour
    changes that may surprise existing users (default-embedder
    swap, new env vars, new CLI subcommands, new retrieval paths).
    Existing indexes may need to be rebuilt or re-embedded.
  - **Patch (0.x.y → 0.x.z)** — bug fixes, doc sync, performance
    work that doesn't change the public surface, internal
    refactors. Existing indexes are byte-compatible.

This release (`0.2.3`) is a patch — pure docs sync, no code path
changed.

## Past lapses (lessons)

  - **0.2.2** shipped its features to PyPI but did not update
    `docs/index.html` or `README.md`. New users could not discover
    the intelligent-recovery and routing-transparency features
    until 0.2.3. This document exists to prevent that recurring.
  - **0.1.0 / 0.2.0 / 0.2.1 / 0.2.2 GitHub Actions PyPI uploads
    all 403'd** because the `PYPI_API_TOKEN` repo secret is unset
    or invalid. The manual `twine upload` flow has been working
    around it. **Fix is tracked but separate work.**
