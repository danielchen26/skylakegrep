# skylakegrep 0.7.4 — Python 3.9 import fix

0.7.4 is a one-line packaging patch. No feature changes.

## What changed

- **`storage.py` postponed annotations (#20).** After #15 added
  `resolve_base: Path | None`, Python 3.9 failed at import/test collection.
  Adding `from __future__ import annotations` restores `requires-python >=3.9`.
  `index` / `watch` behavior is unchanged.

## Compatibility

- Same as 0.7.3 otherwise (Apache-2.0, path-scoped DB). Existing indexes untouched.

## Verification

- Master Tests CI green on Python 3.9–3.12 after #20.
