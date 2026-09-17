# Trademark and naming policy

The Apache License 2.0 covers the **code**. It explicitly does not grant
rights to names, logos, or marks (Section 6). This file states what you
may and may not do with the names.

## Marks

- **skylakegrep** — project name
- **skygrep** — command name
- the wordmark and hero artwork under `docs/assets/`

These are unregistered trademarks of Tianchi Chen.

## You may, without asking

- Use the names to refer to this project truthfully: "built with
  skylakegrep", "skylakegrep integration", "compatible with skygrep".
- Publish articles, tutorials, benchmarks, and comparisons that name it.
- Redistribute unmodified releases under their original name — that is
  what the license is for.
- Package it for a distribution (Homebrew, nixpkgs, Debian, conda-forge)
  under the name `skylakegrep`, with patches limited to packaging.

## You must rename if you fork

If you distribute a **modified** version, use your own name for it. Do
not ship a changed tool that still calls itself `skylakegrep` or answers
to `skygrep`, and do not publish a package under those names on PyPI,
npm, Homebrew, or any other registry.

State the lineage instead:

> `yourtool` — a fork of skylakegrep (https://github.com/danielchen26/skylakegrep)

This is the same rule Python, Rust, and Kubernetes apply, and it exists
for one reason: when someone reports a bug or benchmarks "skylakegrep",
the result must be attributable to this codebase.

## You may not

- Imply endorsement, affiliation, or official status without written
  permission.
- Use the marks in your company name, product name, domain name, or app
  store listing title.
- Register the marks, or confusingly similar marks, anywhere.

## Contact

Questions, or a use you want cleared in writing:
[open an issue](https://github.com/danielchen26/skylakegrep/issues) or
email chentianchi@gmail.com.
