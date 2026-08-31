# SPDX-License-Identifier: Apache-2.0
"""Dependency and network-egress preflight for retrieval tools.

Every retrieval benchmark measures quality on a machine where every tool
already works. That hides the axis that decides enterprise procurement:
**whether the tool can be installed and initialised at all** on a network
that filters outbound traffic.

This is not hypothetical. Measured on a pharmaceutical corporate network on
2026-08-28, ``ck --index`` could not start because its ONNX embedding model
lives on ``huggingface.co``, which the network's SASE gateway blocks by
category; ``ollama pull bge-m3`` succeeded from the same shell. On that
network ck's semantic, lexical, and hybrid modes are all unavailable — its
own help text notes that ``--lex`` is "auto-indexed before it runs", so the
BM25 mode routes through the same blocked fetch — leaving only regex, i.e.
grep. No amount of retrieval quality changes that outcome.

Two rules keep this honest:

**The result describes a network, not just a tool.** A blocked probe is a
fact about where you ran it. Receipts therefore carry a network fingerprint,
and the reachability field is named ``measured_*`` while the dependency
claims are named ``declared_*`` and each carry a source.

**Nothing here circumvents anything.** Probes are plain HEAD requests with
default certificate verification. A TLS failure is recorded as a finding,
never retried with verification disabled, and no mirror or proxy is
attempted. Bypassing an employer's egress policy would invalidate the
measurement and is not the point: the point is that the block exists.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_TIMEOUT = 12.0

#: Egress classes, worst last. ``none`` means a query needs no network at any
#: point after install; ``model-fetch-once`` needs it once to obtain weights;
#: ``per-query`` ships the query, and usually file content, off the machine.
EGRESS_CLASSES = ("none", "model-fetch-once", "per-query")

#: Substrings that mark a captive/filter response body or redirect target.
#: Deliberately generic: SASE vendors differ, the shape does not.
_BLOCK_MARKERS = (
    "block_ai",
    "blockpage",
    "block-page",
    "category_denied",
    "categorydenied",
    "access denied",
    "not allowed to browse",
    "webfilter",
    "url filtering",
    "content filter",
    "zscaler",
    "netskope",
    "forcepoint",
    "sase",
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_USER_PARAM_RE = re.compile(r"((?:user|username|login|uid|email)=)[^&\s]*", re.I)


def redact(text: str) -> str:
    """Strip identity out of filter-page evidence before it is written down.

    Block pages embed the authenticated user — the observed redirect carried
    ``user=<address>`` plus an opaque session token. A benchmark receipt is a
    public artifact, so the useful part (a block happened, by this vendor,
    for this category) is kept and the identifying part is removed.
    """

    text = _EMAIL_RE.sub("<redacted-user>", text)
    text = _USER_PARAM_RE.sub(r"\1<redacted>", text)
    # Long opaque query tokens leak session state; keep the readable reason.
    text = re.sub(r"([?&](?:zsq|token|sig|auth)=)[^&\s]{8,}", r"\1<redacted>", text, flags=re.I)
    return text


@dataclass(frozen=True)
class ToolDependency:
    """What a tool needs from the network, and where that claim comes from."""

    tool: str
    binary: str
    install: str
    #: URLs that must be fetchable for this capability to initialise.
    #: Empty means the capability has no model dependency at all.
    declared_model_urls: tuple[str, ...]
    declared_egress: str
    declared_source: str
    #: Which capability of the tool this row describes. A tool can have more
    #: than one, with different exposure — and the home team is not exempt.
    #: skylakegrep's retrieval works from a local Ollama, but its optional
    #: cross-encoder reranking pulls a model from huggingface.co, the same
    #: domain whose blocking disables ck. An earlier version of this registry
    #: listed only the Ollama dependency, which made the receipt claim
    #: skylakegrep was unconditionally installable when only its default
    #: configuration is.
    profile: str = "base"
    #: True when the capability is an extra rather than the default path, so a
    #: blocked optional profile is not read as the tool failing to install.
    optional: bool = False
    notes: str = ""

    @property
    def label(self) -> str:
        return self.tool if self.profile == "base" else f"{self.tool}[{self.profile}]"

    def __post_init__(self) -> None:
        if self.declared_egress not in EGRESS_CLASSES:
            raise ValueError(
                f"{self.label}: declared_egress must be one of {EGRESS_CLASSES}"
            )
        if not self.declared_source.strip():
            raise ValueError(f"{self.label}: every declared claim needs a source")


REGISTRY: tuple[ToolDependency, ...] = (
    ToolDependency(
        tool="skylakegrep",
        binary="skygrep",
        install="pip install skylakegrep",
        declared_model_urls=("https://registry.ollama.ai/v2/",),
        declared_egress="model-fetch-once",
        declared_source=(
            "skylakegrep/src/config.py DEFAULT_EMBED_MODEL='bge-m3', served by a "
            "local Ollama; queries hit http://localhost:11434 only"
        ),
        notes="Weights arrive via `ollama pull`; search itself is loopback-only.",
    ),
    ToolDependency(
        tool="skylakegrep",
        profile="rerank",
        optional=True,
        binary="skygrep",
        install="pip install 'skylakegrep[rerank]'",
        declared_model_urls=(
            "https://huggingface.co/mixedbread-ai/mxbai-rerank-large-v2/resolve/main/config.json",
        ),
        declared_egress="model-fetch-once",
        declared_source=(
            "skylakegrep/src/config.py DEFAULT_RERANK_MODEL="
            "'mixedbread-ai/mxbai-rerank-large-v2'; cli.py --rerank-model help "
            "says 'HuggingFace cross-encoder model id'"
        ),
        notes=(
            "The reranking path has the same exposure as ck: its weights come "
            "from huggingface.co. Where that domain is blocked, skylakegrep "
            "still retrieves but cannot rerank, so any ranking figure measured "
            "with reranking on is unobtainable there. Note also that the "
            "default cross-encoder is published by Mixedbread, whose mgrep is "
            "the closest competing tool."
        ),
    ),
    ToolDependency(
        tool="ck",
        binary="ck",
        install="cargo install ck-search",
        declared_model_urls=(
            "https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main/onnx/model.onnx",
        ),
        declared_egress="model-fetch-once",
        declared_source=(
            "ck 0.7.11 --index error names the URL it fetches; `ck --help` states "
            "--lex is 'auto-indexed before it runs', so BM25 shares the path"
        ),
        notes="Regex mode needs no index and keeps working when the fetch fails.",
    ),
    ToolDependency(
        tool="mgrep",
        binary="mgrep",
        install="npm install -g @mixedbread/mgrep",
        declared_model_urls=("https://api.mixedbread.com/",),
        declared_egress="per-query",
        declared_source=(
            "mgrep README documents a device-login flow against Mixedbread's "
            "hosted store; mixedbread.com/pricing bills indexing and queries"
        ),
        notes="Repository content is uploaded to a third-party index.",
    ),
    ToolDependency(
        tool="ripgrep",
        binary="rg",
        install="brew install ripgrep",
        declared_model_urls=(),
        declared_egress="none",
        declared_source="No model, no index, no network path in ripgrep",
        notes="Lexical floor. Included because it always survives.",
    ),
)


@dataclass
class ProbeResult:
    url: str
    measured_status: str
    http_status: Optional[int] = None
    detail: str = ""
    seconds: float = 0.0


def classify_response(url: str, status: int, final_url: str, body_head: str) -> ProbeResult:
    """Classify a completed HTTP response. Pure, so it is testable offline.

    The question is reachability, not endpoint semantics. A 404 from
    ``registry.ollama.ai/v2/`` and a 405 from ``api.mixedbread.com`` both mean
    the host answered us, which is the only thing being measured — an earlier
    version of this function scored them as failures and produced a receipt
    claiming every tool was broken. Only a response that came from a
    *different* host, or one carrying filter-page markers, is a block.
    """

    haystack = f"{final_url} {body_head}".lower()
    marker = next((m for m in _BLOCK_MARKERS if m in haystack), None)
    redirected_offsite = _host(final_url) != _host(url)
    if marker or redirected_offsite:
        return ProbeResult(
            url=url,
            measured_status="blocked_by_policy",
            http_status=status,
            detail=redact(
                f"answered by {_host(final_url)} instead of {_host(url)}"
                if redirected_offsite
                else f"filter marker {marker!r}"
            ),
        )
    return ProbeResult(
        url=url,
        measured_status="reachable",
        http_status=status,
        detail="" if 200 <= status < 400 else f"origin answered HTTP {status}",
    )


def _host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except Exception:  # pragma: no cover - urlsplit is total for str input
        return ""


def _probe_via_curl(url: str, timeout: float) -> Optional[ProbeResult]:
    """Probe with the system HTTP client, which sees what the OS sees.

    Necessary, not stylistic. On the measured network ``curl`` is handed an
    immediate ``307`` to the gateway's block page while Python's ``urllib``
    simply times out on the same URL from the same shell — the endpoint
    agent routes traffic per process, and urllib does not follow the system
    proxy/trust path. Reporting "timeout" when the operating system is being
    told "CATEGORY_DENIED" understates the finding, so the probe prefers the
    client that can observe it and records which transport answered.

    Redirects are deliberately not followed: the redirect target *is* the
    evidence, and following it would fetch a page carrying the authenticated
    user's identity.
    """

    curl = shutil.which("curl")
    if not curl:
        return None
    try:
        proc = subprocess.run(
            [
                curl, "-s", "-o", os.devnull,
                "-w", "%{http_code}\\t%{redirect_url}\\t%{url_effective}",
                "--max-time", str(int(timeout)),
                "-r", "0-0",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    parts = (proc.stdout or "").split("\t")
    if len(parts) < 3 or not parts[0].strip().isdigit():
        return None
    status = int(parts[0].strip())
    redirect_url, final_url = parts[1].strip(), parts[2].strip()
    if status == 0:
        return None
    result = classify_response(url, status, redirect_url or final_url, "")
    result.detail = (result.detail + " [transport=curl]").strip()
    return result


def probe(url: str, timeout: float = DEFAULT_TIMEOUT) -> ProbeResult:
    """Fetch one byte of a URL with default TLS verification.

    A single-byte ranged GET rather than HEAD: filtering gateways and CDNs
    handle HEAD inconsistently — the observed SASE gateway silently dropped
    HEAD to a blocked host, so the probe timed out and the receipt said
    "unreachable" when ``curl`` on the same shell was being handed a 307 to a
    block page. GET gets a truthful answer while transferring nothing.

    A TLS or transport failure is recorded and never retried with
    verification disabled; that would measure a network this tool's users do
    not have.
    """

    started = time.perf_counter()
    via_curl = _probe_via_curl(url, timeout)
    if via_curl is not None:
        via_curl.seconds = round(time.perf_counter() - started, 3)
        return via_curl
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Range": "bytes=0-0", "User-Agent": "skylakegrep-preflight"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = ""
            try:
                body = response.read(2048).decode("utf-8", "replace")
            except Exception:
                pass
            result = classify_response(url, response.status, response.geturl(), body)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(2048).decode("utf-8", "replace")
        except Exception:
            pass
        result = classify_response(url, exc.code, exc.url or url, body)
    except ssl.SSLCertVerificationError as exc:
        # A gateway terminating TLS with a private root looks exactly like this.
        result = ProbeResult(
            url=url, measured_status="tls_intercepted", detail=redact(str(exc))
        )
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if isinstance(exc.reason, socket.gaierror):
            status = "dns_failure"
        elif isinstance(exc.reason, (TimeoutError, socket.timeout)):
            status = "timeout"
        elif "certificate" in reason.lower():
            status = "tls_intercepted"
        else:
            status = "unreachable"
        result = ProbeResult(url=url, measured_status=status, detail=redact(reason))
    except (TimeoutError, socket.timeout):
        result = ProbeResult(url=url, measured_status="timeout")
    result.seconds = round(time.perf_counter() - started, 3)
    return result


def network_fingerprint() -> dict[str, Any]:
    """Identify the network a receipt describes, without identifying the user."""

    proxies = sorted(
        key for key in os.environ if key.lower().endswith("_proxy") or key.lower() == "no_proxy"
    )
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "proxy_env_vars_present": proxies,
        "custom_ca_env_vars_present": sorted(
            key
            for key in os.environ
            if any(t in key.upper() for t in ("CA_CERT", "CA_BUNDLE", "SSL_CERT"))
        ),
        "note": (
            "reachability below is a property of this network; rerun on yours "
            "before treating any status as a property of the tool"
        ),
    }



def _binary_present(binary: str) -> bool:
    """PATH plus the directory of the running interpreter.

    A pip-installed console script lives next to ``sys.executable`` inside a
    virtualenv and is absent from PATH unless the venv is activated. Checking
    PATH alone reported skylakegrep's own binary as missing.
    """

    if shutil.which(binary):
        return True
    return (Path(sys.executable).parent / binary).exists()


def evaluate(deps: tuple[ToolDependency, ...] = REGISTRY, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    rows = []
    for dep in deps:
        probes = [asdict(probe(url, timeout)) for url in dep.declared_model_urls]
        blocked = [p for p in probes if p["measured_status"] != "reachable"]
        if not dep.declared_model_urls:
            capability = "not_applicable"
        elif blocked:
            capability = "unavailable"
        else:
            capability = "available"
        rows.append(
            {
                "tool": dep.tool,
                "profile": dep.profile,
                "label": dep.label,
                "optional": dep.optional,
                "declared_egress": dep.declared_egress,
                "declared_source": dep.declared_source,
                "declared_model_urls": list(dep.declared_model_urls),
                "measured_binary_present": _binary_present(dep.binary),
                "measured_model_probes": probes,
                # Named for what it measures: whether this capability can
                # initialise here. It was "measured_semantic_mode" while the
                # registry only described default retrieval paths.
                "measured_capability": capability,
                "install": dep.install,
                "notes": dep.notes,
            }
        )
    return {
        "schema_version": 1,
        "definition": {
            "benchmark": "dependency and network-egress preflight",
            "question": "can this tool initialise semantic search on this network at all",
            "method": "HEAD each declared model URL with default TLS verification",
            "integrity": "no proxy, no mirror, no verification bypass is attempted",
            "privacy": "filter-page evidence is redacted of user identity and tokens",
        },
        "network": network_fingerprint(),
        "tools": rows,
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        f"{'capability':<24}{'binary':>7}{'declared egress':>19}{'works here':>17}",
    ]
    for row in report["tools"]:
        label = row["label"] + (" (opt)" if row["optional"] else "")
        lines.append(
            f"{label:<24}"
            f"{'yes' if row['measured_binary_present'] else 'no':>7}"
            f"{row['declared_egress']:>19}"
            f"{row['measured_capability']:>17}"
        )
    for row in report["tools"]:
        for p in row["measured_model_probes"]:
            if p["measured_status"] != "reachable":
                lines.append(
                    f"  ! {row['label']}: {p['measured_status']} <- {p['url']}"
                    + (f"\n      {p['detail']}" if p["detail"] else "")
                )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="write the JSON receipt here")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    report = evaluate(timeout=args.timeout)
    print(render(report))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
