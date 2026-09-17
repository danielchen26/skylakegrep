# Pre-release: Apache surface cleanup

**Status:** complete on this branch.

Live docs surfaces (landing page, hero, roadmap) now match the Apache-2.0
relicense (#14). Historical versioned notes under `docs/skylakegrep-0.*`
remain archival snapshots and are intentionally untouched.

## Acceptance checklist

- [x] `docs/index.html` meta / og / eyebrow license strings → Apache-2.0
- [x] `docs/assets/hero-dark.svg` badge line → Apache-2.0
- [x] `docs/roadmap.md` + `docs/roadmap.html` obsolete relicensing bullet → Recently done (#14)
- [x] Grep live surfaces (not `skylakegrep-0.*`) for the old noncommercial license name — expect **zero** hits outside this completed checklist note

## Out of scope

- Version bump + PyPI republish (`0.7.3`) — separate release PR
