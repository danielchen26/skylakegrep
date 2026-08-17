use anyhow::{Context, Result};
use sha2::{Digest, Sha256};
use std::{
    env,
    path::{Path, PathBuf},
    process::Command,
};

#[derive(Debug, Clone)]
pub struct SearchConfig {
    pub root: PathBuf,
    pub db_path: PathBuf,
    pub ollama_url: String,
    pub embed_model: String,
    pub top_k: usize,
}

pub fn resolve_search_config(root: Option<PathBuf>) -> Result<SearchConfig> {
    let root = project_root(root)?;
    let db_path = match env::var("SKYGREP_DB_PATH") {
        Ok(path) if !path.trim().is_empty() => PathBuf::from(path),
        _ => project_db_path(&root)?,
    };

    Ok(SearchConfig {
        root,
        db_path,
        ollama_url: env::var("OLLAMA_URL").unwrap_or_else(|_| "http://localhost:11434".to_string()),
        embed_model: env::var("OLLAMA_EMBED_MODEL").unwrap_or_else(|_| "bge-m3".to_string()),
        top_k: 8,
    })
}

fn project_root(start: Option<PathBuf>) -> Result<PathBuf> {
    let base = start.unwrap_or(env::current_dir().context("failed to read current dir")?);
    let base = base.canonicalize().unwrap_or(base);

    if let Ok(output) = Command::new("git")
        .arg("-C")
        .arg(&base)
        .arg("rev-parse")
        .arg("--show-toplevel")
        .output()
    {
        if output.status.success() {
            let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !text.is_empty() {
                return Ok(PathBuf::from(text));
            }
        }
    }

    Ok(base)
}

fn project_db_path(root: &Path) -> Result<PathBuf> {
    let mut hasher = Sha256::new();
    hasher.update(root.to_string_lossy().as_bytes());
    let digest = format!("{:x}", hasher.finalize());
    let suffix = &digest[..8];
    let name = root
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("root")
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
                ch
            } else {
                '_'
            }
        })
        .collect::<String>();
    let home = env::var("HOME").context("HOME is required to derive skygrep db path")?;
    Ok(PathBuf::from(home)
        .join(".skylakegrep")
        .join("repos")
        .join(format!("{name}-{suffix}.db")))
}
