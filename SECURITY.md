# Security Policy

## Supported versions

Security fixes are applied to the latest release on [PyPI](https://pypi.org/project/skylakegrep/) and the `master` branch of this repository.

## Reporting a vulnerability

Please **do not** open a public issue for security reports.

Email the maintainer at the address listed in [`CITATION.cff`](./CITATION.cff) / GitHub profile, or open a private [GitHub Security Advisory](https://github.com/danielchen26/skylakegrep/security/advisories/new) if available.

Include: affected version, impact, and steps to reproduce if possible.

## Automated checks

Dependency updates are automated by
[Dependabot](https://github.com/danielchen26/skylakegrep/blob/master/.github/dependabot.yml), which opens
weekly `pip` and GitHub Actions bump pull requests.

The [`Security` workflow](https://github.com/danielchen26/skylakegrep/blob/master/.github/workflows/security.yml)
runs on every pull request, on `master` pushes that touch packaging, source, or the
security configuration itself, on a weekly Monday schedule, and on manual dispatch. Its jobs:

- [`pip-audit`](https://github.com/danielchen26/skylakegrep/blob/master/.github/workflows/security.yml) — known-vulnerability audit of the resolved dependency tree
- [CodeQL](https://github.com/danielchen26/skylakegrep/blob/master/.github/workflows/security.yml) — static application security testing for Python, results in code scanning
- [Trivy](https://github.com/danielchen26/skylakegrep/blob/master/.github/workflows/security.yml) — filesystem scan for vulnerabilities, secrets, and misconfiguration, results in code scanning; a second secret-only pass fails the build on any committed credential
- [CycloneDX SBOM](https://github.com/danielchen26/skylakegrep/blob/master/.github/workflows/security.yml) — a CycloneDX 1.6 SBOM is generated from the installed environment and published as a build artifact (`sbom-cyclonedx`) on every run

Supply-chain posture of the release path:

- [PyPI Trusted Publishing via OIDC](https://github.com/danielchen26/skylakegrep/blob/master/.github/workflows/release.yml) — releases are published with a short-lived OIDC token (`id-token: write`); no long-lived PyPI API token exists in this repository
- Least-privilege `permissions:` are declared explicitly on every workflow in [`.github/workflows`](https://github.com/danielchen26/skylakegrep/tree/master/.github/workflows)
