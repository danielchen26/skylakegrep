# skylakegrep 0.2.13 — release notes

`0.2.13` is a privacy-only release. **No code changes.** It exists
solely to ship a sanitised README + release notes to PyPI / GitHub
Pages, replacing user-personal references in earlier 0.2.x release
notes with generic placeholders (`<token>`, `<filename-A>.pdf`,
etc.).

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact <chentianchi@gmail.com>.

## What changed

  - `docs/skylakegrep-0.2.7.md` through `0.2.12.md` — sanitised.
    User-personal example query content (a debugging query the
    project author shared with the assistant during development)
    was previously inlined verbatim into the published release
    notes; replaced with generic `<token>` / `<file>` /
    `<filename-A>` placeholders that demonstrate the same
    behaviour without exposing personal data.
  - `docs/index.html` — same sweep.
  - `docs/PRINCIPLES.md` — same sweep.
  - `docs/plans/2026-05-05-conversational-session-state.md` —
    same sweep (the plan referenced the same query as a worked
    example).
  - `README.md` — same sweep.
  - GitHub Release notes pages for `v0.2.7` through `v0.2.12`
    were updated via `gh release edit --notes-file …` to replace
    their published content with the sanitised version.

## What does NOT change

  - **No code change.** `skylakegrep/src/` was already sanitised
    in 0.2.7 — no token strings or personal identifiers are
    present in production code. The leak was confined to release
    notes / docs / plans.
  - Indexes built on 0.2.0–0.2.12 continue to work unchanged.
  - Bench numbers unchanged.
  - Test suite: 201 / 201 passing (unchanged from 0.2.12).

## Lesson recorded

The 0.2.7 sanitisation pass swept production code (per the user's
explicit directive at the time). It did NOT extend the sweep to
release notes, plan documents, or `README.md`. That oversight is
now recorded as an addendum to the existing
`feedback_no_personal_examples_in_code.md` auto-memory entry:

> **Sanitisation must cover all artefacts that ship publicly,
> not just source code.** Release notes, plan documents, README,
> docs/index.html, GitHub Release pages — every public-facing
> surface needs to be checked. Pre-commit `grep -i` on the staged
> diff is the bare-minimum sanity check.

A future-proof guard would be a CI check that fails on any commit
introducing one of a small list of "never-publish" tokens (the
project author's name + a few hand-chosen tokens). Tracked as a
follow-up.

## Compatibility

  - Python ≥ 3.9 (unchanged)
  - Existing 0.2.0–0.2.12 indexes: no migration.
  - Bench numbers unchanged.

## Known follow-ups (not in 0.2.13)

  - **CI never-publish-token guard** — pre-commit hook + CI check
    that fails the build if any commit introduces user-personal
    tokens into public-facing files. Prevents repeat of the
    issue this release fixed.
  - **Conversational session state** —
    [`docs/plans/2026-05-05-conversational-session-state.md`](plans/2026-05-05-conversational-session-state.md).
  - **Graph-prior folder inference** —
    [`docs/plans/2026-05-05-graph-prior-folder-inference.md`](plans/2026-05-05-graph-prior-folder-inference.md).
  - **Phase C** — full intelligent-retrieval audit; tracked in
    [`docs/plans/2026-05-05-phase-c-audit.md`](plans/2026-05-05-phase-c-audit.md).
  - More proactive enhancers (`query_refinement`,
    `markdown_link_traverse`, `pdf_section_extract`,
    `git_history_related`).
  - Re-render `docs/assets/{benchmark,schema,hero-dark,og-image}.svg`
    to reflect bge-m3 defaults.
  - Re-run the self-test bench on bge-m3 and update
    `docs/token-benchmarking.md`.
  - Fix the GitHub Actions `PYPI_API_TOKEN` 403; manual `twine`
    flow continues to work.
