import json
import requests

from .config import get_config


def get_answerer():
    cfg = get_config()
    return OllamaAnswerer(cfg["ollama_url"], cfg["llm_model"])


class OllamaAnswerer:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def hyde(self, query: str, language_hint: str = "") -> str:
        """Generate a hypothetical code answer for ``query`` (HyDE).

        The query embedding is then computed over this hypothetical code rather
        than the natural-language question. For natural-language queries
        ("where is microphone audio captured"), the hypothetical doc usually
        contains the right identifiers (``capture_audio``, ``AudioStream``,
        crate / module names) that bring the actual implementation chunk into
        the cosine top-k. Returns the original query when the LLM call fails
        so callers can wire this in unconditionally.

        The Ollama generation is run with ``temperature=0`` and a fixed
        ``seed`` so the same query produces the same hypothetical doc across
        runs — this matters because HyDE feeds retrieval, and we cannot
        compare benchmark numbers across runs if the generated doc varies.
        """

        lang_part = f" The codebase is written primarily in {language_hint}." if language_hint else ""
        prompt = (
            "Given a natural-language code-search question, write a SHORT "
            "(5-15 lines) hypothetical code snippet — function signatures, "
            "type names, file path comment — that would directly answer it."
            f"{lang_part} Prefer concrete crate / module names that a typical "
            "Rust workspace would use (e.g. ``crates/ai/``, ``crates/editor/``, "
            "``crates/voice_input/``). Output only the code, no explanation, "
            "no markdown fences. Use realistic identifier names that an "
            "engineer would write.\n\n"
            f"Question: {query}\n\n"
            "Hypothetical code:"
        )
        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0, "seed": 42, "num_predict": 256},
                },
                timeout=120,
            )
            response.raise_for_status()
            text = response.json().get("response", "").strip()
            if not text:
                return query
            # Combine the question and the hypothetical doc; the embedding then
            # benefits from both surface-language anchors and the synthesized
            # identifier vocabulary.
            return f"{query}\n\n{text}"
        except requests.RequestException:
            return query

    def decompose(self, query: str, max_queries: int = 3) -> list[str]:
        prompt = (
            "Break this code-search question into up to "
            f"{max_queries} concise local search queries. "
            "Return only a JSON array of strings.\n\n"
            f"Question: {query}"
        )
        response = requests.post(
            f"{self.base_url}/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False},
            timeout=120,
        )
        response.raise_for_status()
        text = response.json().get("response", "").strip()
        try:
            parsed = json.loads(text)
            queries = [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            queries = [line.strip(" -\t") for line in text.splitlines() if line.strip()]
        unique = []
        for item in queries:
            if item not in unique:
                unique.append(item)
        return unique[:max_queries]

    def answer(self, query: str, results: list[dict]) -> str:
        context = "\n\n".join(
            f"[{index}] {result['path']}:{result.get('start_line')}-{result.get('end_line')}\n"
            f"{result['snippet']}"
            for index, result in enumerate(results, start=1)
        )
        prompt = (
            "You are answering a code search question using only local search results. "
            "Be concise, cite file paths and line ranges, say if the answer is not present, "
            "and do not ask follow-up questions.\n\n"
            f"Question: {query}\n\n"
            f"Local search results:\n{context}\n\n"
            "Answer:"
        )
        response = requests.post(
            f"{self.base_url}/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False},
            timeout=120,
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()
