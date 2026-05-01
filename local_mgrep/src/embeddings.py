import requests
from .config import get_config

def get_embedder():
    cfg = get_config()
    return OllamaEmbedder(cfg["ollama_url"], cfg["embed_model"])

class OllamaEmbedder:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def embed(self, text: str) -> list[float]:
        resp = requests.post(
            f"{self.base_url}/api/embeddings",
            json={"model": self.model, "prompt": text},
            timeout=60
        )
        resp.raise_for_status()
        return resp.json()["embedding"]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            resp = requests.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": texts},
                timeout=120
            )
            resp.raise_for_status()
            embeddings = resp.json().get("embeddings")
            if isinstance(embeddings, list) and len(embeddings) == len(texts):
                return embeddings
        except requests.RequestException:
            return [self.embed(t) for t in texts]
        return [self.embed(t) for t in texts]
