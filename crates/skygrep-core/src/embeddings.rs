use anyhow::{anyhow, Context, Result};
use serde::Deserialize;

pub trait Embedder {
    fn embed(&self, text: &str) -> Result<Vec<f32>>;
}

#[derive(Debug, Clone)]
pub struct OllamaEmbedder {
    base_url: String,
    model: String,
}

impl OllamaEmbedder {
    pub fn new(base_url: impl Into<String>, model: impl Into<String>) -> Self {
        Self {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            model: model.into(),
        }
    }
}

#[derive(Debug, Deserialize)]
struct EmbeddingResponse {
    embedding: Vec<f32>,
}

impl Embedder for OllamaEmbedder {
    fn embed(&self, text: &str) -> Result<Vec<f32>> {
        let clipped = if text.len() > 7_500 {
            &text[..7_500]
        } else {
            text
        };
        let body = serde_json::json!({
            "model": self.model,
            "prompt": clipped,
            "keep_alive": "-1"
        });
        let response: EmbeddingResponse = ureq::post(&format!("{}/api/embeddings", self.base_url))
            .send_json(body)
            .context("failed to call Ollama embeddings endpoint")?
            .body_mut()
            .read_json()
            .context("failed to decode Ollama embedding response")?;
        if response.embedding.is_empty() {
            return Err(anyhow!("Ollama returned an empty embedding"));
        }
        Ok(response.embedding)
    }
}
