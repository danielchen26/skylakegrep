"""Tests for the stale-install detector surfaced by ``skygrep doctor``.

A stale installed distribution shadowing the running source is one of
the most confusing failure modes for users: flags appear missing and
already-fixed bugs seem to persist. ``doctor`` must name it explicitly.
"""

from __future__ import annotations

from importlib import metadata as md
from unittest import mock

from skylakegrep.src import cli
from skylakegrep.src import __version__


def test_no_warning_when_versions_agree():
    with mock.patch.object(md, "version", return_value=__version__):
        assert cli._stale_install_warning() is None


def test_no_warning_when_not_installed_as_distribution():
    with mock.patch.object(
        md, "version", side_effect=md.PackageNotFoundError("skylakegrep")
    ):
        assert cli._stale_install_warning() is None


def test_warns_when_installed_version_is_older_than_source():
    with mock.patch.object(md, "version", return_value="0.5.8.5"):
        warning = cli._stale_install_warning()

    assert warning is not None
    assert warning["installed_version"] == "0.5.8.5"
    assert warning["source_version"] == __version__
    # Both versions must appear so the user can see the mismatch.
    assert "0.5.8.5" in warning["summary"]
    assert __version__ in warning["summary"]
    # An actionable remedy is required, not just a diagnosis.
    assert any("pip install" in hint for hint in warning["hints"])


def test_doctor_reports_stale_install(monkeypatch):
    """The warning must actually reach ``doctor`` output."""
    from click.testing import CliRunner

    monkeypatch.setattr(
        cli,
        "_stale_install_warning",
        lambda: {
            "summary": "stale install: source 9.9.9 vs installed 0.0.1",
            "hints": ["reinstall so both agree: pip install -U skylakegrep"],
            "source_version": "9.9.9",
            "installed_version": "0.0.1",
        },
    )
    monkeypatch.setattr(cli, "_auto_refresh_setup_snippets", lambda: None)
    monkeypatch.setattr(
        cli.bootstrap,
        "doctor_report",
        lambda url: {
            "ollama": {"ok": False, "url": url, "error": "probe skipped"},
            "models": [],
        },
    )

    result = CliRunner().invoke(cli.cli, ["doctor"])

    assert "stale install" in result.output
    assert "0.0.1" in result.output
    assert "pip install" in result.output
