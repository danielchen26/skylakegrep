use crate::{
    config::SearchConfig,
    db::init_db,
    embeddings::{Embedder, OllamaEmbedder},
    lexical::lexical_candidate_paths,
};
use anyhow::Result;
use regex::Regex;
use rusqlite::{params_from_iter, Connection, ToSql};
use skygrep_protocol::{CascadeTelemetry, SearchQuality, SearchResult};
use std::collections::{BTreeMap, BTreeSet, HashSet};

const LEXICAL_WEIGHT: f64 = 0.2;
const MAX_RESULTS_PER_FILE: usize = 2;
const CASCADE_DEFAULT_TAU: f64 = 0.015;
const CASCADE_TAU_FLOOR: f64 = 0.005;
const CASCADE_K_SIGMA: f64 = 1.0;
const SYMBOL_WEIGHT: f64 = 0.10;
const TIEBREAK_EPS: f64 = 0.005;
const GRAPH_TIEBREAK_WEIGHT: f64 = 0.005;

#[derive(Debug, Clone)]
pub struct SearchResponse {
    pub results: Vec<SearchResult>,
    pub telemetry: CascadeTelemetry,
    pub indexed_file_count: usize,
    pub candidate_count: usize,
}

#[derive(Debug, Clone)]
struct ChunkRow {
    path: String,
    snippet: String,
    language: Option<String>,
    start_line: Option<i64>,
    end_line: Option<i64>,
    embedding: Vec<f32>,
}

#[derive(Debug, Clone)]
struct Candidate {
    path: String,
    snippet: String,
    language: Option<String>,
    start_line: Option<i64>,
    end_line: Option<i64>,
    score: f64,
    semantic_score: f64,
    lexical_score: f64,
    symbol_boost: Option<f64>,
    graph_tiebreak: bool,
}

pub fn run_search(config: &SearchConfig, query: &str) -> Result<SearchResponse> {
    let conn = init_db(&config.db_path)?;
    let indexed_file_count = indexed_file_count(&conn)?;
    let embedder = OllamaEmbedder::new(&config.ollama_url, &config.embed_model);
    let query_embedding = embedder.embed(query)?;
    let candidates = lexical_candidate_paths(query, &config.root);
    let (results, telemetry) = cascade_search(
        &conn,
        &query_embedding,
        query,
        config.top_k,
        Some(&candidates),
    )?;

    Ok(SearchResponse {
        results,
        telemetry,
        indexed_file_count,
        candidate_count: candidates.len(),
    })
}

fn indexed_file_count(conn: &Connection) -> Result<usize> {
    let count: i64 = conn.query_row("SELECT COUNT(DISTINCT file) FROM chunks", [], |row| {
        row.get(0)
    })?;
    Ok(count as usize)
}

fn cascade_search(
    conn: &Connection,
    query_embedding: &[f32],
    query_text: &str,
    top_k: usize,
    candidate_paths: Option<&BTreeSet<String>>,
) -> Result<(Vec<SearchResult>, CascadeTelemetry)> {
    let pairs = file_level_pairs(conn, query_embedding, top_k.max(10), None)?;
    let gap = if pairs.len() >= 2 {
        pairs[0].1 - pairs[1].1
    } else {
        0.0
    };
    let mut tau = CASCADE_DEFAULT_TAU;
    let mut tau_mode = "static".to_string();
    if pairs.len() >= 3 {
        let scores = pairs
            .iter()
            .take(top_k.max(10))
            .map(|(_, s)| *s)
            .collect::<Vec<_>>();
        let sigma = stddev(&scores);
        tau = CASCADE_TAU_FLOOR.max(CASCADE_K_SIGMA * sigma);
        tau_mode = "adaptive".to_string();
    }
    let early_exit = pairs.len() >= 2 && gap >= tau;
    let mut quality = SearchQuality::Best;

    let results = if early_exit {
        let chosen = pairs
            .iter()
            .map(|(path, _)| path.clone())
            .collect::<BTreeSet<_>>();
        search_chunks(
            conn,
            query_embedding,
            query_text,
            top_k,
            Some(&chosen),
            true,
        )?
    } else {
        quality = SearchQuality::Uncertain;
        let widened = search_chunks(conn, query_embedding, query_text, top_k, None, true)?;
        if widened.is_empty() && candidate_paths.is_some_and(|paths| !paths.is_empty()) {
            search_chunks(
                conn,
                query_embedding,
                query_text,
                top_k,
                candidate_paths,
                true,
            )?
        } else {
            widened
        }
    };

    let telemetry = CascadeTelemetry {
        path: if early_exit {
            "cosine-cheap".to_string()
        } else {
            "cosine-escalated-rerank".to_string()
        },
        gap,
        tau,
        tau_static: CASCADE_DEFAULT_TAU,
        tau_mode,
        early_exit,
        quality: if results.is_empty() {
            SearchQuality::Uncertain
        } else {
            quality
        },
    };
    Ok((results, telemetry))
}

fn file_level_pairs(
    conn: &Connection,
    query_embedding: &[f32],
    top_files: usize,
    candidate_paths: Option<&BTreeSet<String>>,
) -> Result<Vec<(String, f64)>> {
    let rows = if let Some(paths) = candidate_paths {
        if paths.is_empty() {
            Vec::new()
        } else {
            let placeholders = std::iter::repeat("?")
                .take(paths.len())
                .collect::<Vec<_>>()
                .join(",");
            let sql = format!("SELECT file, embedding FROM files WHERE file IN ({placeholders})");
            let values = paths.iter().map(|s| s as &dyn ToSql).collect::<Vec<_>>();
            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt.query_map(params_from_iter(values), |row| {
                let path: String = row.get(0)?;
                let blob: Vec<u8> = row.get(1)?;
                Ok((path, blob))
            })?;
            rows.collect::<std::result::Result<Vec<_>, _>>()?
        }
    } else {
        let mut stmt = conn.prepare("SELECT file, embedding FROM files")?;
        let rows = stmt.query_map([], |row| {
            let path: String = row.get(0)?;
            let blob: Vec<u8> = row.get(1)?;
            Ok((path, blob))
        })?;
        rows.collect::<std::result::Result<Vec<_>, _>>()?
    };

    let mut scored = rows
        .into_iter()
        .filter_map(|(path, blob)| blob_to_vec(&blob, query_embedding.len()).map(|vec| (path, vec)))
        .map(|(path, vec)| (path, cosine(query_embedding, &vec)))
        .collect::<Vec<_>>();
    scored.sort_by(|a, b| b.1.total_cmp(&a.1));
    scored.truncate(top_files);
    Ok(scored)
}

fn search_chunks(
    conn: &Connection,
    query_embedding: &[f32],
    query_text: &str,
    top_k: usize,
    candidate_paths: Option<&BTreeSet<String>>,
    rank_by_file: bool,
) -> Result<Vec<SearchResult>> {
    let rows = load_chunk_rows(conn, query_embedding.len(), candidate_paths)?;
    if rows.is_empty() {
        return Ok(Vec::new());
    }

    let mut seen = HashSet::new();
    let mut candidates = rows
        .into_iter()
        .filter_map(|row| {
            let semantic = cosine(query_embedding, &row.embedding);
            let lexical = lexical_score(query_text, &row.path, &row.snippet);
            let score = combine_scores(semantic, lexical);
            let key = (
                row.path.clone(),
                row.start_line,
                row.end_line,
                row.snippet.clone(),
            );
            if !seen.insert(key) {
                return None;
            }
            Some(Candidate {
                path: row.path,
                snippet: row.snippet,
                language: row.language,
                start_line: row.start_line,
                end_line: row.end_line,
                score,
                semantic_score: semantic,
                lexical_score: lexical,
                symbol_boost: None,
                graph_tiebreak: false,
            })
        })
        .collect::<Vec<_>>();

    candidates.sort_by(|a, b| b.score.total_cmp(&a.score));
    apply_non_canonical_penalty(&mut candidates);
    apply_symbol_boost(conn, query_text, &mut candidates)?;
    apply_graph_tiebreak(conn, &mut candidates)?;

    let selected = if rank_by_file {
        rank_files(candidates, top_k)
    } else {
        diversify(candidates, top_k)
    };

    Ok(selected.into_iter().map(candidate_to_result).collect())
}

fn load_chunk_rows(
    conn: &Connection,
    expected_dim: usize,
    candidate_paths: Option<&BTreeSet<String>>,
) -> Result<Vec<ChunkRow>> {
    let mut params: Vec<&dyn ToSql> = Vec::new();
    let where_clause = if let Some(paths) = candidate_paths {
        if paths.is_empty() {
            return Ok(Vec::new());
        }
        params.extend(paths.iter().map(|s| s as &dyn ToSql));
        format!(
            "WHERE chunks.file IN ({})",
            std::iter::repeat("?")
                .take(paths.len())
                .collect::<Vec<_>>()
                .join(",")
        )
    } else {
        String::new()
    };

    let sql = format!(
        r#"
        SELECT chunks.id, file, chunk, language, start_line, end_line, embedding
        FROM chunks JOIN vectors ON vectors.id = chunks.id
        {where_clause}
        "#
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt
        .query_map(params_from_iter(params), |row| {
            let blob: Vec<u8> = row.get(6)?;
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, Option<String>>(3)?,
                row.get::<_, Option<i64>>(4)?,
                row.get::<_, Option<i64>>(5)?,
                blob,
            ))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;

    Ok(rows
        .into_iter()
        .filter_map(
            |(_id, path, snippet, language, start_line, end_line, blob)| {
                blob_to_vec(&blob, expected_dim).map(|embedding| ChunkRow {
                    path,
                    snippet,
                    language,
                    start_line,
                    end_line,
                    embedding,
                })
            },
        )
        .collect())
}

fn blob_to_vec(blob: &[u8], expected_dim: usize) -> Option<Vec<f32>> {
    if blob.len() != expected_dim * 4 {
        return None;
    }
    let mut out = Vec::with_capacity(expected_dim);
    for chunk in blob.chunks_exact(4) {
        out.push(f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]));
    }
    Some(out)
}

fn cosine(a: &[f32], b: &[f32]) -> f64 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }
    let mut dot = 0.0_f64;
    let mut na = 0.0_f64;
    let mut nb = 0.0_f64;
    for (x, y) in a.iter().zip(b.iter()) {
        let x = *x as f64;
        let y = *y as f64;
        dot += x * y;
        na += x * x;
        nb += y * y;
    }
    dot / ((na.sqrt() * nb.sqrt()) + 1e-8)
}

fn tokenize_search_text(text: &str) -> Vec<String> {
    let camel_re = Regex::new(r"([a-z0-9])([A-Z])").expect("valid camel regex");
    let token_re = Regex::new(r"[a-z0-9]+").expect("valid token regex");
    let spaced = camel_re.replace_all(text, "$1 $2").to_lowercase();
    token_re
        .find_iter(&spaced)
        .map(|m| m.as_str().to_string())
        .collect()
}

fn lexical_score(query_text: &str, path: &str, chunk: &str) -> f64 {
    let query_tokens = tokenize_search_text(query_text);
    if query_tokens.is_empty() {
        return 0.0;
    }
    let query_terms = query_tokens
        .iter()
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    let target_tokens = tokenize_search_text(&format!("{path} {chunk}"));
    let target_terms = target_tokens.iter().cloned().collect::<BTreeSet<_>>();
    if target_terms.is_empty() {
        return 0.0;
    }
    let term_score = query_terms
        .iter()
        .filter(|term| target_terms.contains(*term))
        .count() as f64
        / query_terms.len() as f64;
    let phrase_score = if target_tokens.join(" ").contains(&query_tokens.join(" ")) {
        1.0
    } else {
        0.0
    };
    (term_score * 0.75 + phrase_score * 0.25).min(1.0)
}

fn combine_scores(semantic_score: f64, lexical_score: f64) -> f64 {
    semantic_score * (1.0 - LEXICAL_WEIGHT) + lexical_score * LEXICAL_WEIGHT
}

fn apply_non_canonical_penalty(candidates: &mut [Candidate]) {
    let markers = [
        "_test.rs",
        "_tests.rs",
        "_test.py",
        "_tests.py",
        "/tests/",
        "/test/",
        "/fixtures/",
        "/examples/",
        "/vendor/",
        "/node_modules/",
        "/dist/",
        "/build/",
        "/target/",
        ".min.js",
    ];
    let mut changed = false;
    for candidate in candidates.iter_mut() {
        let path = format!("/{}", candidate.path.to_lowercase());
        if markers.iter().any(|marker| path.contains(marker)) {
            candidate.score *= 0.5;
            changed = true;
        }
    }
    if changed {
        candidates.sort_by(|a, b| b.score.total_cmp(&a.score));
    }
}

fn apply_symbol_boost(
    conn: &Connection,
    query_text: &str,
    candidates: &mut [Candidate],
) -> Result<()> {
    if candidates.is_empty() {
        return Ok(());
    }
    let query_terms = tokenize_search_text(query_text)
        .into_iter()
        .filter(|term| term.len() >= 4)
        .collect::<BTreeSet<_>>();
    if query_terms.is_empty() {
        return Ok(());
    }
    let paths = candidates
        .iter()
        .map(|candidate| candidate.path.clone())
        .collect::<BTreeSet<_>>();
    let placeholders = std::iter::repeat("?")
        .take(paths.len())
        .collect::<Vec<_>>()
        .join(",");
    let sql = format!("SELECT file, name_lower FROM symbols WHERE file IN ({placeholders})");
    let values = paths.iter().map(|s| s as &dyn ToSql).collect::<Vec<_>>();
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt
        .query_map(params_from_iter(values), |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let mut matched: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for (path, name_lower) in rows {
        let tokens = name_lower
            .split_whitespace()
            .map(|s| s.to_string())
            .collect::<BTreeSet<_>>();
        for term in &query_terms {
            if tokens.contains(term) {
                matched
                    .entry(path.clone())
                    .or_default()
                    .insert(term.clone());
            }
        }
    }
    if matched.is_empty() {
        return Ok(());
    }
    for candidate in candidates.iter_mut() {
        if let Some(terms) = matched.get(&candidate.path) {
            let bump = terms.len() as f64 / query_terms.len() as f64 * SYMBOL_WEIGHT;
            candidate.score += bump;
            candidate.symbol_boost = Some(bump);
        }
    }
    candidates.sort_by(|a, b| b.score.total_cmp(&a.score));
    Ok(())
}

fn apply_graph_tiebreak(conn: &Connection, candidates: &mut [Candidate]) -> Result<()> {
    if candidates.len() < 2 || candidates[0].score - candidates[1].score >= TIEBREAK_EPS {
        return Ok(());
    }
    let take = candidates.len().min(5);
    let paths = candidates[..take]
        .iter()
        .map(|candidate| candidate.path.clone())
        .collect::<Vec<_>>();
    let placeholders = std::iter::repeat("?")
        .take(paths.len())
        .collect::<Vec<_>>()
        .join(",");
    let sql = format!("SELECT file, pagerank FROM file_graph WHERE file IN ({placeholders})");
    let values = paths.iter().map(|s| s as &dyn ToSql).collect::<Vec<_>>();
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt
        .query_map(params_from_iter(values), |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, f64>(1)?))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    if rows.is_empty() {
        return Ok(());
    }
    let pagerank = rows.into_iter().collect::<BTreeMap<_, _>>();
    let values = paths
        .iter()
        .map(|path| *pagerank.get(path).unwrap_or(&0.0))
        .collect::<Vec<_>>();
    let lo = values.iter().copied().fold(f64::INFINITY, f64::min);
    let hi = values.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let span = (hi - lo) + 1e-9;
    for candidate in &mut candidates[..take] {
        let pr = *pagerank.get(&candidate.path).unwrap_or(&0.0);
        candidate.score += GRAPH_TIEBREAK_WEIGHT * ((pr - lo) / span);
        candidate.graph_tiebreak = true;
    }
    candidates.sort_by(|a, b| b.score.total_cmp(&a.score));
    Ok(())
}

fn rank_files(candidates: Vec<Candidate>, top_k: usize) -> Vec<Candidate> {
    let mut best: BTreeMap<String, Candidate> = BTreeMap::new();
    for candidate in candidates {
        match best.get(&candidate.path) {
            Some(existing) if existing.score >= candidate.score => {}
            _ => {
                best.insert(candidate.path.clone(), candidate);
            }
        }
    }
    let mut ranked = best.into_values().collect::<Vec<_>>();
    ranked.sort_by(|a, b| b.score.total_cmp(&a.score));
    ranked.truncate(top_k);
    ranked
}

fn diversify(candidates: Vec<Candidate>, top_k: usize) -> Vec<Candidate> {
    let mut selected = Vec::new();
    let mut overflow = Vec::new();
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    for candidate in candidates {
        let count = counts.entry(candidate.path.clone()).or_default();
        if *count < MAX_RESULTS_PER_FILE {
            *count += 1;
            selected.push(candidate);
            if selected.len() >= top_k {
                return selected;
            }
        } else {
            overflow.push(candidate);
        }
    }
    for candidate in overflow {
        selected.push(candidate);
        if selected.len() >= top_k {
            break;
        }
    }
    selected
}

fn candidate_to_result(candidate: Candidate) -> SearchResult {
    SearchResult {
        path: candidate.path,
        start_line: candidate.start_line,
        end_line: candidate.end_line,
        language: candidate.language,
        score: candidate.score,
        semantic_score: Some(candidate.semantic_score),
        lexical_score: Some(candidate.lexical_score),
        symbol_boost: candidate.symbol_boost,
        source_type: if candidate.graph_tiebreak {
            "graph-adjusted".to_string()
        } else if candidate.symbol_boost.unwrap_or(0.0) > 0.0 {
            "symbol".to_string()
        } else {
            "semantic".to_string()
        },
        snippet: candidate.snippet,
    }
}

fn stddev(values: &[f64]) -> f64 {
    if values.len() < 2 {
        return 0.0;
    }
    let mean = values.iter().sum::<f64>() / values.len() as f64;
    let variance = values.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / values.len() as f64;
    variance.sqrt()
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    fn vec_blob(values: &[f32]) -> Vec<u8> {
        values
            .iter()
            .flat_map(|value| value.to_le_bytes())
            .collect::<Vec<_>>()
    }

    fn fixture_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        crate::db::init_schema(&conn).unwrap();
        for (id, file, chunk, vec) in [
            (
                1,
                "src/search.rs",
                "fn measure_token_savings() {}",
                vec![1.0, 0.0],
            ),
            (
                2,
                "tests/search_test.rs",
                "fn test_measure_token_savings() {}",
                vec![0.95, 0.0],
            ),
            (3, "src/ui.rs", "fn render_overlay() {}", vec![0.0, 1.0]),
        ] {
            conn.execute(
                "INSERT INTO chunks (id, file, chunk, language, chunk_index, start_line, end_line) VALUES (?, ?, ?, 'rust', ?, 1, 3)",
                (id, file, chunk, id),
            )
            .unwrap();
            conn.execute(
                "INSERT INTO vectors (id, embedding) VALUES (?, ?)",
                (id, vec_blob(&vec)),
            )
            .unwrap();
        }
        conn.execute(
            "INSERT INTO files (file, chunk_count, embedding) VALUES ('src/search.rs', 1, ?)",
            [vec_blob(&[1.0, 0.0])],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO files (file, chunk_count, embedding) VALUES ('tests/search_test.rs', 1, ?)",
            [vec_blob(&[0.95, 0.0])],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols (file, name, name_lower, kind) VALUES ('src/search.rs', 'measure_token_savings', 'measure token savings', 'function')",
            [],
        )
        .unwrap();
        conn
    }

    #[test]
    fn cosine_ranking_prefers_matching_chunk() {
        let conn = fixture_conn();
        let results =
            search_chunks(&conn, &[1.0, 0.0], "measure token savings", 5, None, true).unwrap();
        assert_eq!(results[0].path, "src/search.rs");
    }

    #[test]
    fn symbol_boost_marks_symbol_result() {
        let conn = fixture_conn();
        let results =
            search_chunks(&conn, &[1.0, 0.0], "measure token savings", 5, None, true).unwrap();
        assert_eq!(results[0].source_type, "symbol");
        assert!(results[0].symbol_boost.unwrap() > 0.0);
    }

    #[test]
    fn graph_tiebreak_changes_near_ties() {
        let conn = fixture_conn();
        conn.execute(
            "INSERT INTO file_graph (file, pagerank) VALUES ('src/search.rs', 0.1), ('tests/search_test.rs', 0.9)",
            [],
        )
        .unwrap();
        let mut candidates = vec![
            Candidate {
                path: "src/search.rs".to_string(),
                snippet: String::new(),
                language: None,
                start_line: None,
                end_line: None,
                score: 0.8,
                semantic_score: 0.8,
                lexical_score: 0.0,
                symbol_boost: None,
                graph_tiebreak: false,
            },
            Candidate {
                path: "tests/search_test.rs".to_string(),
                snippet: String::new(),
                language: None,
                start_line: None,
                end_line: None,
                score: 0.799,
                semantic_score: 0.799,
                lexical_score: 0.0,
                symbol_boost: None,
                graph_tiebreak: false,
            },
        ];
        apply_graph_tiebreak(&conn, &mut candidates).unwrap();
        assert!(candidates.iter().any(|candidate| candidate.graph_tiebreak));
    }
}
