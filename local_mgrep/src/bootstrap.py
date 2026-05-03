"""Runtime bootstrap: detect Ollama, probe embed/LLM models, optionally pull.

This module is the friendly front door of ``mgrep``. The first time a user
runs the CLI we want to:

  - Confirm the local Ollama runtime is reachable.
  - Confirm the embedding model the CLI is configured to use is present.
  - Optionally confirm the LLM (used by ``--hyde`` / ``--answer`` / cascade
    escalation) is present.

When a check fails we either:
  - print a single actionable command the user can copy-paste, or
  - run ``ollama pull <model>`` ourselves (with confirm prompt unless
    ``MGREP_AUTO_PULL=yes`` is set).

The probes are cheap (one HTTP GET each); we don't gate ``mgrep search`` on
them when the index is already populated and reachable, because that adds
latency to every query. Bootstrap is invoked from ``cli.doctor`` (always),
from ``cli.search_cmd`` only when an actual error needs explaining (e.g.
embed call returned an empty body), and from ``auto_index.ensure_index``
before the first index of a fresh project.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Iterable

import requests

from .config import get_config

logger = logging.getLogger(__name__)

OLLAMA_INSTALL_HINT = (
    "Ollama is required for local embeddings. Install on macOS:\n"
    "    brew install ollama\n"
    "or follow https://ollama.com/download. After install, start the server:\n"
    "    ollama serve  &"
)


class BootstrapError(RuntimeError):
    """Raised when a required runtime/model is missing and we cannot fix it."""


def _probe_ollama(base_url: str, timeout: float = 2.0) -> tuple[bool, str]:
    """Return (reachable, error message)."""
    try:
        r = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
        r.raise_for_status()
        return True, ""
    except requests.RequestException as exc:
        return False, str(exc)


def list_local_models(base_url: str, timeout: float = 5.0) -> list[str]:
    """Return the list of locally-installed Ollama model names (with tags).

    Tags are normalised so that ``nomic-embed-text`` matches a server entry
    of ``nomic-embed-text:latest``.
    """
    try:
        r = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
        r.raise_for_status()
    except requests.RequestException:
        return []
    data = r.json() or {}
    out: list[str] = []
    for item in data.get("models", []) or []:
        name = item.get("name") or item.get("model") or ""
        if name:
            out.append(name)
    return out


def _model_present(installed: Iterable[str], wanted: str) -> bool:
    """Match ``nomic-embed-text`` against ``nomic-embed-text:latest``."""
    wanted_base = wanted.split(":", 1)[0]
    for m in installed:
        base = m.split(":", 1)[0]
        if m == wanted or base == wanted_base:
            return True
    return False


def pull_model(model: str, *, base_url: str | None = None, stream: bool = True) -> bool:
    """Run ``ollama pull <model>`` via the HTTP API, stream progress to stderr.

    Returns True on success. On failure prints actionable error and returns
    False.
    """

    cfg = get_config()
    url = (base_url or cfg["ollama_url"]).rstrip("/")
    print(f"  → pulling {model} (this may take a few minutes) …", file=sys.stderr, flush=True)
    try:
        with requests.post(
            f"{url}/api/pull",
            json={"name": model},
            stream=True,
            timeout=None,
        ) as r:
            r.raise_for_status()
            last = ""
            for raw in r.iter_lines():
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg = obj.get("status", "")
                # Show only meaningful transitions, not byte-by-byte progress.
                if msg and msg != last and not msg.startswith("downloading"):
                    print(f"    {msg}", file=sys.stderr, flush=True)
                    last = msg
                if obj.get("error"):
                    print(f"  × pull error: {obj['error']}", file=sys.stderr, flush=True)
                    return False
    except requests.RequestException as exc:
        print(
            f"  × pull failed: {exc}\n"
            f"    Try manually: ollama pull {model}",
            file=sys.stderr,
        )
        return False
    print(f"  ✓ pulled {model}", file=sys.stderr, flush=True)
    return True


def ensure_ollama(base_url: str | None = None) -> None:
    """Raise BootstrapError with an actionable message if Ollama is unreachable."""
    cfg = get_config()
    url = (base_url or cfg["ollama_url"])
    ok, err = _probe_ollama(url)
    if not ok:
        raise BootstrapError(
            f"Ollama not reachable at {url}: {err}\n\n{OLLAMA_INSTALL_HINT}"
        )


def ensure_model(
    model: str,
    *,
    base_url: str | None = None,
    auto_pull: bool | None = None,
    confirm_prompt: str | None = None,
) -> None:
    """Ensure an Ollama model is locally present, pulling on demand.

    ``auto_pull=True`` skips the y/N prompt. ``auto_pull=None`` reads
    ``MGREP_AUTO_PULL`` (``yes`` / ``no``). Default is to prompt.
    """

    cfg = get_config()
    url = (base_url or cfg["ollama_url"])
    installed = list_local_models(url)
    if _model_present(installed, model):
        return

    if auto_pull is None:
        env = os.environ.get("MGREP_AUTO_PULL", "").lower()
        auto_pull = env in {"yes", "y", "true", "1"}

    if not auto_pull:
        prompt = confirm_prompt or (
            f"Model '{model}' is not installed locally. Pull it now? [Y/n] "
        )
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            answer = ""
        if answer and answer not in {"y", "yes"}:
            raise BootstrapError(
                f"Aborted. Pull manually with: ollama pull {model}"
            )

    if not pull_model(model, base_url=url):
        raise BootstrapError(
            f"Could not pull '{model}'. Try manually: ollama pull {model}"
        )


def doctor_report(base_url: str | None = None) -> dict:
    """Collect a structured health report. Used by ``mgrep doctor``."""
    cfg = get_config()
    url = (base_url or cfg["ollama_url"]).rstrip("/")
    report: dict = {
        "ollama": {"url": url, "ok": False, "error": ""},
        "models": [],
        "keep_alive": cfg.get("keep_alive"),
    }
    ok, err = _probe_ollama(url)
    report["ollama"]["ok"] = ok
    report["ollama"]["error"] = err
    if not ok:
        return report
    installed = list_local_models(url)
    seen_names: set[str] = set()
    for label, name in (
        ("embed", cfg["embed_model"]),
        ("llm (--answer)", cfg["llm_model"]),
        ("llm (cascade/HyDE)", cfg.get("hyde_model") or cfg["llm_model"]),
    ):
        if name in seen_names:
            # ``hyde_model == llm_model`` is fine (legacy / explicit override) —
            # don't print the same row twice.
            continue
        seen_names.add(name)
        report["models"].append(
            {
                "role": label,
                "name": name,
                "present": _model_present(installed, name),
            }
        )
    return report
