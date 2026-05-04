# skylakegrep 0.16.0 — release notes

## ⚠️ License change

**This release changes the license from MIT to PolyForm Noncommercial 1.0.0.**

### What changes

- ✅ **Personal, academic, research, hobby, and any other non-commercial
  use is fully permitted** — including modification, redistribution,
  and use inside non-profit / educational / governmental
  organisations.
- ❌ **Commercial use is NOT permitted under this license.** This
  includes use inside any for-profit company, embedding in
  commercial products, SaaS resale, etc. Commercial users must
  obtain a separate commercial license.
- 📧 To obtain a commercial license, open a GitHub issue titled
  "Commercial license inquiry" or email <chentianchi@gmail.com>.

### What about earlier versions?

Earlier releases (v0.2.0 – v0.15.1) were originally distributed under
the MIT License at release time. Two changes:

1. **Git history has been rewritten** so every checked-in `LICENSE`
   file (including in historical tags) now reflects the new
   PolyForm NC 1.0.0 license. Anyone cloning the repo from now on
   sees the new license throughout history.
2. **Old PyPI versions are being removed.** Only v0.16.0+ will be
   available on PyPI under the new license. Binaries already
   downloaded under MIT remain MIT in the wild — that legal grant
   cannot be retroactively revoked — but new installs go to v0.16.0.

The `LICENSE` file in this repository explicitly preserves a
historical note about the MIT-era pre-v0.16.0 binaries; we are not
trying to deny that history, only to control the license going
forward.

### Why?

Going forward I want personal / academic / research use to remain
free, while companies that want to embed skylakegrep in commercial
products engage with me directly for licensing. PolyForm
Noncommercial 1.0.0 is a software-specific, lawyer-drafted license
(by Heather Meeker) that makes this distinction cleanly.

## What did NOT change

The retrieval pipeline, CLI flags, JSON output, file format, and
all behaviour from v0.15.1 are byte-for-byte identical. This is
**a license-only release**.

## Files changed

  - `LICENSE` — replaced full text with PolyForm Noncommercial 1.0.0,
    plus required notice + commercial-license contact.
  - `pyproject.toml` — `license = "MIT"` → `license = {file =
    "LICENSE"}`; version 0.15.1 → 0.16.0.
  - `README.md` — license badge updated, prominent NOTICE block
    added under the License section.
  - `docs/index.html` — `og:description`, page eyebrow, `<meta>`
    description all updated; v0.15.1 → v0.16.0.
  - `docs/assets/og-image.svg` + `og-image.png` (re-rendered) —
    "MIT" → "NC license"; version stamp.
  - `docs/assets/hero-dark.svg` — "MIT" → "PolyForm-NC-1.0.0";
    version stamp.
  - `docs/skylakegrep-0.16.0.md` (this file).
  - `docs/README.md` — index entry for v0.16.0.

## Compatibility

  - **120 / 120 unit tests pass** (no code changes; license-only).
  - All 0.4.x – 0.15.1 flags / env / per-project DB layout
    unchanged.
  - JSON output: byte-for-byte v0.15.1.

## Install

```
pip install --upgrade skylakegrep
```

If you are using skylakegrep in a commercial context and want to
continue receiving updates, please contact me before upgrading to
v0.16.0+ so we can arrange a commercial license.
