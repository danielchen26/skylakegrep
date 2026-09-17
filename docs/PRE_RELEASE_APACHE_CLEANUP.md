# Pre-release: Apache surface cleanup (contributor task)

**Why:** GitHub `master` is already Apache-2.0 (#14) and the index path bug is fixed (#15), but several **live docs surfaces** still say PolyForm Noncommercial. That blocks a clean production-facing release narrative (and confuses anyone landing on GitHub Pages).

**Do not touch:** historical versioned notes under `docs/skylakegrep-0.*.md|html` — those are archival snapshots of past releases.

## Acceptance checklist

- [x] `docs/index.html` meta / og / eyebrow license strings → Apache-2.0 (starter done in this PR)
- [ ] `docs/assets/hero-dark.svg` badge line: `PolyForm-NC-1.0.0` → `Apache-2.0` (and bump the shown version if you want it current)
- [ ] `docs/roadmap.md` + `docs/roadmap.html`: replace the obsolete “MIT relicensing / PolyForm NC / commercial license” bullet with a short “Relicensed to Apache-2.0 (#14)” note
- [ ] Grep live surfaces (not `skylakegrep-0.*`) for `PolyForm` / `Noncommercial` — expect **zero** hits
- [ ] PR description links this checklist and shows the grep proof

## Out of scope (Code Lead / release owner)

- Version bump + PyPI republish (`0.7.3`) — separate release PR
- Claiming “production open source” before PyPI license metadata matches GitHub

Thanks for picking this up before release.
