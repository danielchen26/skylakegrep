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
