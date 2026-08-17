mod config;
mod db;
mod embeddings;
mod lexical;
mod search;

pub use config::{resolve_search_config, SearchConfig};
pub use db::init_db;
pub use embeddings::{Embedder, OllamaEmbedder};
pub use search::{run_search, SearchResponse};
