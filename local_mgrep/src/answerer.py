import json
import requests

from .config import get_config


def get_answerer():
    cfg = get_config()
    return OllamaAnswerer(
        cfg["ollama_url"],
        cfg["llm_model"],
        hyde_model=cfg.get("hyde_model"),
        keep_alive=cfg.get("keep_alive"),
    )


class OllamaAnswerer:
    def __init__(
        self,
        base_url: str,
        model: str,
        hyde_model: str | None = None,
        keep_alive: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        # ``hyde_model`` lets cascade escalation use a smaller / faster
        # model than ``--answer`` while leaving the answer-quality model
        # untouched. Falls back to ``model`` so existing tests that only
        # pass ``model`` keep working unchanged.
        self.hyde_model = hyde_model or model
        self.keep_alive = keep_alive

    def _options(self) -> dict:
        opts = {"temperature": 0, "seed": 42, "num_predict": 256}
        return opts

    def _payload(self, model: str, prompt: str, *, options: dict | None = None) -> dict:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": options if options is not None else self._options(),
        }
        if self.keep_alive is not None and self.keep_alive != "":
            payload["keep_alive"] = self.keep_alive
        return payload

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
        text = self._generate(self.hyde_model, prompt, fallback=self.model)
        if not text:
            return query
        # Combine the question and the hypothetical doc; the embedding then
        # benefits from both surface-language anchors and the synthesized
        # identifier vocabulary.
        return f"{query}\n\n{text}"

    _missing_logged: set[str] = set()

    def _generate(self, model: str, prompt: str, *, fallback: str | None = None) -> str:
        """POST to /api/generate with graceful fallback when the requested
        model is not installed locally. Returns the response text or empty
        string on permanent failure.
        """
        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json=self._payload(model, prompt),
                timeout=120,
            )
            response.raise_for_status()
            return response.json().get("response", "").strip()
        except requests.HTTPError as exc:
            # Ollama returns 404 with body containing "model not found"
            # when the requested model isn't pulled. Fall back once,
            # logging a hint the first time per model.
            try:
                body = exc.response.text  # type: ignore[union-attr]
            except Exception:
                body = ""
            if (
                fallback
                and fallback != model
                and ("not found" in body.lower() or exc.response is not None and exc.response.status_code == 404)  # type: ignore[union-attr]
            ):
                if model not in self._missing_logged:
                    self._missing_logged.add(model)
                    import sys
                    print(
                        f"[mgrep] hyde model {model!r} not installed; falling back to "
                        f"{fallback!r}. Pull the smaller model for faster cascade "
                        f"escalations:  ollama pull {model}",
                        file=sys.stderr,
                    )
                return self._generate(fallback, prompt, fallback=None)
            return ""
        except requests.RequestException:
            return ""

    def decompose(self, query: str, max_queries: int = 3) -> list[str]:
        prompt = (
            "Break this code-search question into up to "
            f"{max_queries} concise local search queries. "
            "Return only a JSON array of strings.\n\n"
            f"Question: {query}"
        )
        response = requests.post(
            f"{self.base_url}/api/generate",
            json=self._payload(self.model, prompt, options={}),
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
            json=self._payload(self.model, prompt, options={}),
            timeout=120,
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()
