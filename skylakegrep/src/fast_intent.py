"""Small local intent substrate for cheap routing decisions.

This module is deliberately not a keyword router. It keeps a tiny set of
generic intent prototypes, embeds query/prototype text into a character
n-gram vector space, then accepts only high-margin decisions. The output
is policy only: retrieval still happens in the normal filename / lexical /
semantic tiers, and uncertainty returns ``None`` so the LLM router or the
safe all-runs fallback can take over.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re


@dataclass(frozen=True)
class FastIntent:
    intent: str
    confidence: float
    primary_token: str = ""
    reason: str = ""


_WORD_RE = re.compile(r"[A-Za-z0-9._-]{2,80}")
_IDENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,80}")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]{2,32}")
_EDGE_QUOTES = "\"'“”‘’`"


_PROTOTYPES: dict[str, tuple[str, ...]] = {
    "filename": (
        "locate a named local file by basename or document identifier",
        "find which folder contains a specific file or attachment",
        "where is a particular file stored",
        "show where a named report or brief is stored",
        "find the manuscript draft by title words",
        "locate a document using descriptive basename words",
        "open the file named by this identifier",
        "查找本地文件路径 根据文件名或文档编号",
        "找到某个文件在哪里",
        "我的文件在哪",
        "文件路径在哪里",
        "查找某个文档的位置",
        "檔案在哪裡",
        "localiser un fichier nommé par identifiant",
        "buscar archivo por nombre",
    ),
    "semantic": (
        "explain how code behavior works and why it happens",
        "explain named identifier logic",
        "explain a project report logic",
        "describe implementation logic or data flow",
        "where is behavior implemented in the code",
        "where is runtime logic applied in the implementation",
        "where is timeout logic applied in code",
        "where is the code path that handles a process",
        "where does a policy get applied in the system",
        "how does this system decide what to do",
        "how are constraints or budgets enforced",
        "what policy governs retries limits and attempts",
        "what does this document say about a process",
        "how does a field or attribute work",
        "what does an identifier field mean in the implementation",
        "why does this function return that result",
        "trace the call flow through the implementation",
        "解释代码如何工作 为什么这样实现",
        "解释这个函数为什么返回结果",
        "说明功能逻辑和调用流程",
        "这个流程是怎么决定的",
        "这个摘要说明了什么流程",
        "摘要说明了什么 renewal process",
        "说明文档内容和相关过程",
        "expliquer le comportement du code",
    ),
    "lexical": (
        "search exact code symbol identifier token literal",
        "grep for function name class variable constant",
        "查找精确代码符号 字面 token",
    ),
    "metadata_opened": (
        "show the files I opened most recently",
        "latest opened files",
        "recently opened files",
        "files opened yesterday",
        "list recently accessed or last used local documents",
        "which documents did I open today",
        "最近打开过的文件",
        "我刚使用过的文档",
        "archivos abiertos recientemente",
    ),
    "metadata_modified": (
        "show recently modified local files",
        "list newest changed or edited files by timestamp",
        "which files were updated today",
        "最近修改过的文件",
        "今天编辑过的文档",
        "archivos modificados recientemente",
    ),
    "metadata_created": (
        "show recently created local files",
        "list newest files by creation time",
        "which document did I create today",
        "files I recently made or wrote",
        "最近创建的文件",
        "今天新建的文档",
        "archivos creados recientemente",
    ),
    "metadata_size": (
        "show largest files by size",
        "list biggest or smallest local files",
        "which files take the most disk space",
        "最大文件",
        "按大小列出文件",
    ),
}


_MIN_SCORE = {
    "filename": 0.035,
    "semantic": 0.080,
    "metadata_opened": 0.050,
    "metadata_modified": 0.050,
    "metadata_created": 0.050,
    "metadata_size": 0.050,
}
_MIN_MARGIN = {
    "filename": 0.020,
    "semantic": 0.050,
    "metadata_opened": 0.050,
    "metadata_modified": 0.050,
    "metadata_created": 0.040,
    "metadata_size": 0.050,
}


def _features(text: str) -> Counter[str]:
    text = (text or "").strip().lower()
    chars = [c for c in text if not c.isspace()]
    out: Counter[str] = Counter()
    for n, weight in ((2, 1), (3, 2), (4, 2)):
        for i in range(max(0, len(chars) - n + 1)):
            out["".join(chars[i:i + n])] += weight
    for word in _WORD_RE.findall(text):
        out[f"w:{word}"] += 3
    return out


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    an = math.sqrt(sum(v * v for v in a.values()))
    bn = math.sqrt(sum(v * v for v in b.values()))
    if not an or not bn:
        return 0.0
    return dot / (an * bn)


def _build_centroids() -> dict[str, Counter[str]]:
    centroids: dict[str, Counter[str]] = {}
    for intent, examples in _PROTOTYPES.items():
        c: Counter[str] = Counter()
        for example in examples:
            c.update(_features(example))
        centroids[intent] = c
    return centroids


_CENTROIDS = _build_centroids()


def _identifier_score(token: str) -> int:
    score = 0
    if any(ch.isdigit() for ch in token):
        score += 100
    if any(ch in "._-/\\" for ch in token):
        score += 80
    if token != token.lower() and token != token.upper():
        score += 20
    if len(token) >= 5:
        score += min(len(token), 20)
    return score


def is_pathlike_candidate(token: str) -> bool:
    return _identifier_score(token) >= 80


def _quoted_spans(text: str) -> list[str]:
    spans: list[str] = []
    current: list[str] = []
    in_quote = False
    for ch in text:
        if ch in _EDGE_QUOTES:
            if in_quote and current:
                spans.append("".join(current).strip())
                current = []
            in_quote = not in_quote
            continue
        if in_quote:
            current.append(ch)
    return [s for s in spans if len(s) >= 2]


def _cjk_ngrams(text: str) -> list[str]:
    out: list[str] = []
    for run in _CJK_RUN_RE.findall(text):
        max_n = min(6, len(run))
        # Prefer likely basename-sized spans over whole natural-language
        # clauses. This is script-level n-gramming, not wrapper stripping.
        lengths = [4, 3, 2, 5, 6]
        for n in lengths:
            if n > max_n:
                continue
            for i in range(0, len(run) - n + 1):
                out.append(run[i:i + n])
    return out


def filename_candidates(
    query: str,
    primary_token: str | None = None,
    *,
    max_candidates: int = 32,
) -> list[str]:
    """Return generic filename-match candidates, strongest first.

    Candidate extraction is intentionally language-agnostic:
      - model/LLM-provided primary token, if any;
      - quoted spans;
      - ASCII/path-like identifier tokens;
      - CJK character n-grams.

    It does not strip language-specific wrappers such as "my", "file",
    "where", or their non-English equivalents. The downstream filename
    tier validates candidates against actual basenames before returning.
    """

    raw: list[str] = []
    if primary_token and primary_token.strip():
        raw.append(primary_token.strip())
    raw.extend(_quoted_spans(query))
    raw.extend(_IDENT_RE.findall(query))
    if primary_token:
        raw.extend(_IDENT_RE.findall(primary_token))
        raw.extend(_cjk_ngrams(primary_token))
    raw.extend(_cjk_ngrams(query))

    deduped: list[str] = []
    seen: set[str] = set()
    for token in raw:
        token = token.strip().strip(_EDGE_QUOTES)
        if len(token) < 2:
            continue
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(token)

    def rank(token: str) -> tuple[int, int, str]:
        is_cjk = 1 if _CJK_RUN_RE.fullmatch(token) else 0
        return (-_identifier_score(token), -is_cjk, -len(token), token.casefold())

    prefix: list[str] = []
    if primary_token:
        primary_clean = primary_token.strip().strip(_EDGE_QUOTES)
        if primary_clean:
            for i, token in enumerate(deduped):
                if token.casefold() == primary_clean.casefold():
                    prefix = [token]
                    deduped.pop(i)
                    break
    return (prefix + sorted(deduped, key=rank))[:max_candidates]


def best_filename_token(query: str) -> str:
    """Return a display token only when the clue is structurally explicit."""

    for token in filename_candidates(query):
        if is_pathlike_candidate(token):
            return token
    return ""


def classify_fast_intent(query: str) -> FastIntent | None:
    """Classify an obvious query intent without invoking an LLM.

    Returns ``None`` unless the top intent has enough absolute score and
    enough margin over the runner-up. Only filename and semantic decisions
    and metadata decisions are accepted here; lexical stays with the
    LLM/fallback router because a false fast lexical decision would be
    more harmful than useful.
    """

    if not query or not query.strip():
        return None
    qv = _features(query)
    scores = {
        intent: _cosine(qv, centroid)
        for intent, centroid in _CENTROIDS.items()
    }
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ranked:
        return None
    intent, score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = score - second
    pathlike_primary = best_filename_token(query)
    pathlike_forced_filename = False
    if (
        pathlike_primary
        and intent == "semantic"
        and scores.get("filename", 0.0) >= _MIN_SCORE["filename"]
        and len(_IDENT_RE.findall(query)) <= 4
        and margin < 0.100
    ):
        intent = "filename"
        score = scores["filename"]
        second = scores.get("semantic", second)
        margin = max(0.0, score - second)
        pathlike_forced_filename = True
    if intent.startswith("metadata_"):
        best_non_metadata = max(
            (v for k, v in scores.items() if not k.startswith("metadata_")),
            default=0.0,
        )
        # Metadata subtypes can be close to one another ("opened" vs
        # "modified") while the metadata-vs-content decision is still
        # clear. Gate metadata on the margin against non-metadata lanes.
        margin = score - best_non_metadata
    if intent not in {
        "filename", "semantic",
        "metadata_opened", "metadata_modified", "metadata_created",
        "metadata_size",
    }:
        return None
    if intent == "semantic" and score < 0.10 and len(_IDENT_RE.findall(query)) <= 3:
        return None
    required_margin = _MIN_MARGIN[intent]
    if pathlike_forced_filename:
        required_margin = 0.0
    if intent == "semantic" and any(
        any(ch in token for ch in "._-/") for token in _IDENT_RE.findall(query)
    ):
        # Structured identifiers often contain substrings that look like
        # metadata words (created_at, modified_time). When the semantic
        # centroid is still clearly on top, do not fall through to an LLM just
        # because the metadata centroid is also nearby.
        required_margin = 0.0 if score >= 0.18 else min(required_margin, 0.020)
    if score < _MIN_SCORE[intent] or margin < required_margin:
        return None

    confidence = min(0.95, 0.55 + score + margin * 2.0)
    if intent == "filename":
        public_intent = "filename"
        primary = best_filename_token(query)
    elif intent.startswith("metadata_"):
        public_intent = "metadata"
        primary = intent.removeprefix("metadata_")
    else:
        public_intent = intent
        primary = ""
    return FastIntent(
        intent=public_intent,
        primary_token=primary,
        confidence=confidence,
        reason=(
            f"fast intent substrate matched {intent} prototypes "
            f"(score={score:.3f}, margin={margin:.3f})"
        ),
    )
