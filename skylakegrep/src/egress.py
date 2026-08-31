# SPDX-License-Identifier: Apache-2.0
"""What network egress this configuration requires, and how to remove it.

skylakegrep already runs its retrieval against a local Ollama, and its
reranker already accepts a filesystem path. What it could not do was *prove*
either of those to someone who has to sign off on it. This module answers one
question with a checkable answer:

    does answering a query require reaching the public internet, and if so,
    exactly which dependency reaches where, and what makes it stop?

That distinction decides procurement in regulated environments, and it is not
hypothetical. Measured on a pharmaceutical corporate network on 2026-08-28,
``huggingface.co`` is blocked by category, which disables ck entirely and
disables skylakegrep's optional reranker. ``registry.ollama.ai`` was reachable
from the same shell — but only because that vendor's category list has not
classified it yet. An advantage that rests on someone else's category list is
not an advantage you can put in a contract, so the goal here is a
configuration whose egress class is ``none`` by construction rather than by
luck: models already on disk, an Ollama on loopback or an internal host, and
nothing that dials out on a query.

``SKYGREP_OFFLINE=1`` turns that from a description into an enforced
constraint: the Hugging Face stack is pinned offline before it can be
imported, and a dependency that would need a public fetch becomes a startup
error naming the fix instead of a stall against a filter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

#: Egress classes, ordered from safest to most exposed.
#:
#: ``loopback`` and ``internal-host`` are both "no public egress", but they are
#: not the same review question: one keeps traffic on the machine, the other
#: puts it on the corporate network. Collapsing them would hide the difference
#: from the person who has to approve it.
EGRESS_NONE = "none"
EGRESS_LOOPBACK = "loopback"
EGRESS_INTERNAL = "internal-host"
EGRESS_PUBLIC = "public-fetch"
EGRESS_ORDER = (EGRESS_NONE, EGRESS_LOOPBACK, EGRESS_INTERNAL, EGRESS_PUBLIC)

OFFLINE_ENV = "SKYGREP_OFFLINE"

#: Environment variables that pin the Hugging Face stack offline. Set before
#: ``sentence_transformers`` is imported, they make it use only cached weights
#: and fail fast instead of hanging against a filter.
_HF_OFFLINE_ENV = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", ""})


@dataclass(frozen=True)
class Dependency:
    """One thing this configuration needs, and where it comes from."""

    name: str
    #: Human-readable resolved source, e.g. ``ollama://localhost:11434 (bge-m3)``.
    source: str
    egress: str
    #: True when the artifact is already on this machine, so no fetch is due.
    satisfied_locally: bool
    #: Copy-pasteable command that removes the egress, or "" when there is none
    #: to remove.
    remedy: str = ""
    optional: bool = False

    @property
    def blocks_offline(self) -> bool:
        """Would enforcing offline mode fail because of this dependency?"""

        return self.egress == EGRESS_PUBLIC and not self.satisfied_locally


def offline_requested() -> bool:
    value = os.environ.get(OFFLINE_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _url_egress(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return EGRESS_LOOPBACK if host in _LOOPBACK_HOSTS else EGRESS_INTERNAL


def hf_cache_root() -> Path:
    """Where the Hugging Face hub cache lives, honouring the documented vars."""

    explicit = os.environ.get("HUGGINGFACE_HUB_CACHE") or os.environ.get("HF_HUB_CACHE")
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def hf_model_is_cached(repo_id: str, cache_root: Optional[Path] = None) -> bool:
    """True when a repo id already has a populated hub cache directory.

    The hub layout is ``models--<org>--<name>`` with weights under
    ``snapshots/``. An empty or refs-only directory is treated as not cached,
    because a partial download is exactly the state that would trigger a fetch.
    """

    root = cache_root if cache_root is not None else hf_cache_root()
    entry = root / ("models--" + repo_id.replace("/", "--"))
    snapshots = entry / "snapshots"
    if not snapshots.is_dir():
        return False
    return any(child.is_dir() and any(child.iterdir()) for child in snapshots.iterdir())


def _looks_like_path(model: str) -> bool:
    """Distinguish a filesystem model directory from a hub repo id.

    Explicit rather than clever: a repo id like ``org/name`` also contains a
    path separator, so existence on disk is what settles it.
    """

    if model.startswith(("/", "./", "../", "~")):
        return True
    return Path(model).expanduser().exists()


def _ollama_models(url: str, timeout: float = 3.0) -> Optional[set[str]]:
    """Tags served by an Ollama instance, or ``None`` when it cannot be asked.

    ``None`` means "unknown", which is deliberately different from "empty":
    a stopped Ollama must not be reported as a missing model.
    """

    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=timeout) as r:
            payload = json.load(r)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    names = set()
    for model in payload.get("models") or []:
        name = str(model.get("name") or "")
        if name:
            names.add(name)
    return names



def _tag_present(model: str, served: set[str]) -> bool:
    """Exact-tag membership. Tags are different weights, not aliases.

    A previous version also matched on the family before the colon, so
    ``qwen2.5:3b`` was reported present on a machine that only had
    ``qwen2.5:7b`` — a false clean bill of health, and in the direction that
    flatters the tool. Ollama resolves a bare name to ``:latest``, so that one
    equivalence is honoured and no other.
    """

    if model in served:
        return True
    if ":" not in model:
        return f"{model}:latest" in served
    if model.endswith(":latest"):
        return model[: -len(":latest")] in served
    return False


def audit(config: dict[str, Any], *, probe_ollama: bool = True) -> list[Dependency]:
    """Describe every model dependency of ``config`` and its egress."""

    url = str(config.get("ollama_url") or "")
    served = _ollama_models(url) if probe_ollama else None
    host_egress = _url_egress(url)

    deps: list[Dependency] = []
    for label, key in (("embedder", "embed_model"), ("router llm", "llm_model")):
        model = str(config.get(key) or "")
        if not model:
            continue
        if served is None:
            present = False
        else:
            present = _tag_present(model, served)
        deps.append(
            Dependency(
                name=label,
                source=f"ollama://{urlsplit(url).netloc or url} ({model})",
                egress=host_egress,
                satisfied_locally=present,
                remedy="" if present else (
                    f"ollama pull {model}   # or side-load: "
                    f"printf 'FROM ./{model}.gguf' > Modelfile && "
                    f"ollama create {model} -f Modelfile"
                ),
            )
        )

    rerank_model = str(config.get("rerank_model") or "")
    if rerank_model:
        if _looks_like_path(rerank_model):
            deps.append(
                Dependency(
                    name="reranker",
                    source=f"file:{rerank_model}",
                    egress=EGRESS_NONE,
                    satisfied_locally=Path(rerank_model).expanduser().exists(),
                    remedy="",
                    optional=True,
                )
            )
        else:
            cached = hf_model_is_cached(rerank_model)
            deps.append(
                Dependency(
                    name="reranker",
                    source=f"hf:{rerank_model}",
                    # A cached repo id still needs no fetch, but the configured
                    # source is public, so the class stays public-fetch and
                    # `satisfied_locally` carries the nuance.
                    egress=EGRESS_PUBLIC,
                    satisfied_locally=cached,
                    remedy=(
                        f"SKYGREP_RERANK_MODEL=/path/to/{rerank_model.split('/')[-1]}"
                        "   # a local directory removes the public dependency"
                    ),
                    optional=True,
                )
            )
    return deps


def worst_egress(deps: list[Dependency]) -> str:
    worst = EGRESS_NONE
    for dep in deps:
        if EGRESS_ORDER.index(dep.egress) > EGRESS_ORDER.index(worst):
            worst = dep.egress
    return worst


class OfflineViolation(RuntimeError):
    """Offline mode was demanded but a dependency would need a public fetch."""


def pin_hf_offline(env: Optional[dict[str, str]] = None) -> None:
    """Pin the Hugging Face stack offline.

    Must run before ``sentence_transformers`` is imported: those libraries read
    these variables at import time. Existing values are preserved so a caller
    who deliberately set them keeps control.
    """

    target = env if env is not None else os.environ
    for name in _HF_OFFLINE_ENV:
        target.setdefault(name, "1")


def enforce(config: dict[str, Any], *, probe_ollama: bool = True) -> list[Dependency]:
    """Apply offline mode, raising :class:`OfflineViolation` if it cannot hold.

    Returns the audit so a caller can report it. Optional dependencies are not
    exempt: an operator who asked for offline mode wants to be told that
    reranking will not work here, not to discover it mid-query.
    """

    pin_hf_offline()
    deps = audit(config, probe_ollama=probe_ollama)
    offenders = [dep for dep in deps if dep.blocks_offline]
    if offenders:
        parts = []
        for dep in offenders:
            parts.append(f"  - {dep.name}: {dep.source} would need a public fetch")
            if dep.remedy:
                parts.append(f"    fix: {dep.remedy}")
        detail = "\n".join(parts)
        raise OfflineViolation(
            f"{OFFLINE_ENV} is set but this configuration is not offline-ready:\n"
            f"{detail}\n"
            f"Unset {OFFLINE_ENV} to allow fetching, or apply the fixes above."
        )
    return deps


def render(deps: list[Dependency], *, offline: Optional[bool] = None) -> list[str]:
    """Lines for ``skygrep doctor``."""

    is_offline = offline_requested() if offline is None else offline
    lines = [f"egress posture: {worst_egress(deps)}" + ("  [SKYGREP_OFFLINE enforced]" if is_offline else "")]
    for dep in deps:
        mark = "✓" if dep.satisfied_locally else ("×" if dep.blocks_offline else "—")
        tag = " (optional)" if dep.optional else ""
        lines.append(f"  {mark} {dep.name}{tag}: {dep.source} [{dep.egress}]")
        if dep.remedy and not dep.satisfied_locally:
            lines.append(f"      fix: {dep.remedy}")
    return lines
