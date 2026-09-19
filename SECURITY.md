# Security Policy

## Supported versions

Security fixes are applied to the latest release on [PyPI](https://pypi.org/project/skylakegrep/) and the `master` branch of this repository.

## Reporting a vulnerability

Please **do not** open a public issue for security reports.

Email the maintainer at the address listed in [`CITATION.cff`](./CITATION.cff) / GitHub profile, or open a private [GitHub Security Advisory](https://github.com/danielchen26/skylakegrep/security/advisories/new) if available.

Include: affected version, impact, and steps to reproduce if possible.

## Automated checks

This repo runs:

- [Dependabot](https://github.com/danielchen26/skylakegrep/blob/master/.github/dependabot.yml) (pip + GitHub Actions)
- `pip-audit` and CodeQL via [`.github/workflows/security.yml`](./.github/workflows/security.yml)
