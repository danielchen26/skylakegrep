use regex::Regex;
use std::{collections::BTreeSet, path::Path, process::Command};

const STOPWORDS: &[&str] = &[
    "the", "a", "an", "is", "for", "to", "of", "and", "that", "this", "where", "what", "how",
    "does", "in", "on", "with", "from", "are", "be", "can", "do", "not", "its", "as", "by", "at",
    "or", "but", "all", "any", "if", "when", "which", "who", "whose",
];

pub fn extract_query_terms(query: &str, max_terms: usize) -> Vec<String> {
    let token_re = Regex::new(r"[A-Za-z_][A-Za-z0-9_]+").expect("valid token regex");
    let mut seen = BTreeSet::new();
    let mut out = Vec::new();
    for mat in token_re.find_iter(query) {
        let term = mat.as_str().to_lowercase();
        if term.len() < 4 || STOPWORDS.contains(&term.as_str()) || seen.contains(&term) {
            continue;
        }
        seen.insert(term.clone());
        out.push(term);
        if out.len() >= max_terms {
            break;
        }
    }
    out
}

pub fn lexical_candidate_paths(query: &str, root: &Path) -> BTreeSet<String> {
    let mut paths = BTreeSet::new();
    if !root.exists() {
        return paths;
    }
    for term in extract_query_terms(query, 8) {
        let output = Command::new("rg")
            .arg("-il")
            .arg("-F")
            .arg(&term)
            .arg(root)
            .output();
        let Ok(output) = output else {
            continue;
        };
        if !output.status.success() && output.stdout.is_empty() {
            continue;
        }
        for line in String::from_utf8_lossy(&output.stdout).lines() {
            let line = line.trim();
            if !line.is_empty() {
                paths.insert(line.to_string());
            }
        }
    }
    paths
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_agentic_terms() {
        assert_eq!(
            extract_query_terms("how does token savings work for Claude agents", 8),
            vec!["token", "savings", "work", "claude", "agents"]
        );
    }
}
