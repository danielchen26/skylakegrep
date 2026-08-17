use anyhow::Result;
use chrono::Utc;
use skygrep_core::{run_search, SearchConfig};
use skygrep_protocol::{
    AgentPhase, LogLevel, OutputPreview, RunSummary, SearchQuality, SuggestedAction, WorkflowEdge,
    WorkflowEvent, WorkflowGraph, WorkflowNode,
};
use std::{
    fs,
    path::{Path, PathBuf},
};

pub fn run_workflow(config: SearchConfig, query: &str) -> Vec<WorkflowEvent> {
    let mut events = Vec::new();
    events.extend(start_events(&config, query));
    events.extend(finish_events(config, query));
    events
}

pub fn start_events(config: &SearchConfig, query: &str) -> Vec<WorkflowEvent> {
    let mut events = Vec::new();
    push(
        &mut events,
        WorkflowEvent::CommandSubmitted {
            query: query.to_string(),
            at: now(),
        },
    );
    log(
        &mut events,
        LogLevel::Info,
        "Parsing request and preparing local workflow",
    );
    push(
        &mut events,
        WorkflowEvent::PhaseChanged {
            phase: AgentPhase::Search,
            progress: 0.12,
        },
    );
    push(
        &mut events,
        WorkflowEvent::SearchStarted {
            query: query.to_string(),
            roots: vec![config.root.display().to_string()],
        },
    );
    push(
        &mut events,
        WorkflowEvent::WorkflowGraphUpdated {
            graph: base_graph("searching", "pending", "pending", "pending"),
        },
    );
    log(
        &mut events,
        LogLevel::Info,
        &format!("Searching local repo at {}", config.root.display()),
    );
    events
}

pub fn finish_events(config: SearchConfig, query: &str) -> Vec<WorkflowEvent> {
    let mut events = Vec::new();
    match run_search(&config, query) {
        Ok(response) => {
            let quality = response.telemetry.quality.clone();
            push(
                &mut events,
                WorkflowEvent::TelemetryUpdated {
                    telemetry: response.telemetry.clone(),
                    indexed_file_count: response.indexed_file_count,
                    candidate_count: response.candidate_count,
                },
            );
            for result in &response.results {
                push(
                    &mut events,
                    WorkflowEvent::SearchResultAdded {
                        result: result.clone(),
                    },
                );
            }
            if response.results.is_empty() {
                log(
                    &mut events,
                    LogLevel::Warn,
                    "No indexed matches returned; check indexing status or refine the query",
                );
            } else if quality == SearchQuality::Uncertain {
                log(
                    &mut events,
                    LogLevel::Warn,
                    "Search completed with low confidence; suggestions include targeted follow-ups",
                );
            } else {
                log(
                    &mut events,
                    LogLevel::Info,
                    &format!(
                        "Retrieved {} ranked result(s), router={}, gap={:.4}, tau={:.4}",
                        response.results.len(),
                        response.telemetry.path,
                        response.telemetry.gap,
                        response.telemetry.tau
                    ),
                );
            }
            push(
                &mut events,
                WorkflowEvent::WorkflowGraphUpdated {
                    graph: base_graph("done", "running", "pending", "pending"),
                },
            );
            push(
                &mut events,
                WorkflowEvent::PhaseChanged {
                    phase: AgentPhase::Analyze,
                    progress: 0.46,
                },
            );
            log(
                &mut events,
                LogLevel::Info,
                "Analyzing ranked candidates and source evidence",
            );

            for suggestion in suggestions(query, &response.results, quality.clone()) {
                push(&mut events, WorkflowEvent::SuggestionAdded { suggestion });
            }

            push(
                &mut events,
                WorkflowEvent::WorkflowGraphUpdated {
                    graph: base_graph("done", "done", "running", "pending"),
                },
            );
            push(
                &mut events,
                WorkflowEvent::PhaseChanged {
                    phase: AgentPhase::Synthesize,
                    progress: 0.74,
                },
            );
            log(
                &mut events,
                LogLevel::Info,
                "Synthesizing output preview from local results",
            );

            let output = output_preview(query, &response.results, quality.clone());
            push(&mut events, WorkflowEvent::OutputPreviewUpdated { output });

            push(
                &mut events,
                WorkflowEvent::WorkflowGraphUpdated {
                    graph: base_graph("done", "done", "done", "done"),
                },
            );
            push(
                &mut events,
                WorkflowEvent::PhaseChanged {
                    phase: AgentPhase::Output,
                    progress: 1.0,
                },
            );
            log(&mut events, LogLevel::Info, "Workflow complete");
            push(&mut events, WorkflowEvent::RunCompleted { quality });
        }
        Err(error) => {
            events.extend(fallback_events(
                &config,
                query,
                &format!("Semantic route unavailable ({error})"),
            ));
        }
    }
    events
}

pub fn fallback_events(config: &SearchConfig, query: &str, reason: &str) -> Vec<WorkflowEvent> {
    let mut events = Vec::new();
    let fallback = fallback_results(&config.root, query, config.top_k);
    if !fallback.is_empty() {
        log(
            &mut events,
            LogLevel::Warn,
            &format!("{reason}; using fast lexical fallback over local files"),
        );
        push(
            &mut events,
            WorkflowEvent::TelemetryUpdated {
                telemetry: skygrep_protocol::CascadeTelemetry {
                    path: "lexical-filesystem-fallback".to_string(),
                    gap: fallback_gap(&fallback),
                    tau: 0.0,
                    tau_static: 0.0,
                    tau_mode: "fallback".to_string(),
                    early_exit: true,
                    quality: SearchQuality::Degraded,
                },
                indexed_file_count: count_candidate_files(&config.root),
                candidate_count: fallback.len(),
            },
        );
        for result in &fallback {
            push(
                &mut events,
                WorkflowEvent::SearchResultAdded {
                    result: result.clone(),
                },
            );
        }
        log(
            &mut events,
            LogLevel::Info,
            &format!(
                "Fallback returned {} immediately inspectable result(s)",
                fallback.len()
            ),
        );
        push(
            &mut events,
            WorkflowEvent::WorkflowGraphUpdated {
                graph: base_graph("done", "running", "pending", "pending"),
            },
        );
        push(
            &mut events,
            WorkflowEvent::PhaseChanged {
                phase: AgentPhase::Analyze,
                progress: 0.5,
            },
        );
        for suggestion in suggestions(query, &fallback, SearchQuality::Degraded) {
            push(&mut events, WorkflowEvent::SuggestionAdded { suggestion });
        }
        push(
            &mut events,
            WorkflowEvent::PhaseChanged {
                phase: AgentPhase::Synthesize,
                progress: 0.78,
            },
        );
        push(
            &mut events,
            WorkflowEvent::OutputPreviewUpdated {
                output: output_preview(query, &fallback, SearchQuality::Degraded),
            },
        );
        push(
            &mut events,
            WorkflowEvent::WorkflowGraphUpdated {
                graph: base_graph("done", "done", "done", "done"),
            },
        );
        push(
            &mut events,
            WorkflowEvent::PhaseChanged {
                phase: AgentPhase::Output,
                progress: 1.0,
            },
        );
        log(
            &mut events,
            LogLevel::Info,
            "Workflow complete with fallback evidence",
        );
        push(
            &mut events,
            WorkflowEvent::RunCompleted {
                quality: SearchQuality::Degraded,
            },
        );
        return events;
    }

    push(
        &mut events,
        WorkflowEvent::WorkflowGraphUpdated {
            graph: base_graph("blocked", "pending", "pending", "pending"),
        },
    );
    log(
        &mut events,
        LogLevel::Error,
        &format!("{reason}. Confirm Ollama is running and the repo is indexed."),
    );
    push(
        &mut events,
        WorkflowEvent::SuggestionAdded {
            suggestion: SuggestedAction {
                id: "check-ollama".to_string(),
                title: "Check local model runtime".to_string(),
                description: "Start Ollama and pull bge-m3 before running semantic search."
                    .to_string(),
                confidence: 0.92,
                action: "ollama pull bge-m3".to_string(),
            },
        },
    );
    push(
        &mut events,
        WorkflowEvent::OutputPreviewUpdated {
            output: OutputPreview {
                title: "Search unavailable".to_string(),
                summary: "Skygrep Desktop could not run the Rust semantic search path yet."
                    .to_string(),
                readiness: "Blocked".to_string(),
                sources: Vec::new(),
            },
        },
    );
    push(
        &mut events,
        WorkflowEvent::RunCompleted {
            quality: SearchQuality::Uncertain,
        },
    );
    events
}

pub fn run_summary(config: SearchConfig, query: &str) -> Result<RunSummary> {
    let response = run_search(&config, query)?;
    Ok(RunSummary {
        query: query.to_string(),
        quality: response.telemetry.quality.clone(),
        results: response.results,
        telemetry: Some(response.telemetry),
    })
}

fn push(events: &mut Vec<WorkflowEvent>, event: WorkflowEvent) {
    events.push(event);
}

fn log(events: &mut Vec<WorkflowEvent>, level: LogLevel, message: &str) {
    push(
        events,
        WorkflowEvent::LogAppended {
            level,
            message: message.to_string(),
            at: now(),
        },
    );
}

fn now() -> String {
    Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

fn base_graph(search: &str, analyze: &str, synthesize: &str, output: &str) -> WorkflowGraph {
    WorkflowGraph {
        id: "default-run".to_string(),
        nodes: vec![
            WorkflowNode {
                id: "interpret".to_string(),
                label: "Interpret Request".to_string(),
                detail: "NLP intent".to_string(),
                phase: AgentPhase::Search,
                state: search.to_string(),
            },
            WorkflowNode {
                id: "search".to_string(),
                label: "Search & Gather".to_string(),
                detail: "Repo + index".to_string(),
                phase: AgentPhase::Search,
                state: search.to_string(),
            },
            WorkflowNode {
                id: "analyze".to_string(),
                label: "Analyze".to_string(),
                detail: "Rank evidence".to_string(),
                phase: AgentPhase::Analyze,
                state: analyze.to_string(),
            },
            WorkflowNode {
                id: "synthesize".to_string(),
                label: "Synthesize".to_string(),
                detail: "Build brief".to_string(),
                phase: AgentPhase::Synthesize,
                state: synthesize.to_string(),
            },
            WorkflowNode {
                id: "output".to_string(),
                label: "Generate Output".to_string(),
                detail: "Preview".to_string(),
                phase: AgentPhase::Output,
                state: output.to_string(),
            },
        ],
        edges: vec![
            WorkflowEdge {
                from: "interpret".to_string(),
                to: "search".to_string(),
            },
            WorkflowEdge {
                from: "search".to_string(),
                to: "analyze".to_string(),
            },
            WorkflowEdge {
                from: "analyze".to_string(),
                to: "synthesize".to_string(),
            },
            WorkflowEdge {
                from: "synthesize".to_string(),
                to: "output".to_string(),
            },
        ],
    }
}

fn suggestions(
    query: &str,
    results: &[skygrep_protocol::SearchResult],
    quality: SearchQuality,
) -> Vec<SuggestedAction> {
    let top = results.first();
    let mut out = Vec::new();
    if quality == SearchQuality::Uncertain {
        out.push(SuggestedAction {
            id: "refine-query".to_string(),
            title: "Refine the search".to_string(),
            description: format!("Try a narrower query based on: {query}"),
            confidence: 0.78,
            action: "refine_query".to_string(),
        });
    }
    if let Some(result) = top {
        out.push(SuggestedAction {
            id: "open-top-result".to_string(),
            title: "Open top result".to_string(),
            description: format!("Inspect {} around the returned line range.", result.path),
            confidence: result.score.clamp(0.0, 1.0),
            action: "open_result".to_string(),
        });
        out.push(SuggestedAction {
            id: "summarize-file".to_string(),
            title: "Summarize candidate file".to_string(),
            description: "Create a concise explanation grounded in the ranked snippets."
                .to_string(),
            confidence: 0.84,
            action: "summarize_file".to_string(),
        });
        out.push(SuggestedAction {
            id: "proactive-followup".to_string(),
            title: "Trace the evidence chain".to_string(),
            description: format!(
                "Proactive route: inspect {}, then compare adjacent ranked candidates.",
                result.path
            ),
            confidence: (result.score * 0.95).clamp(0.0, 1.0),
            action: "proactive_search".to_string(),
        });
    }
    out.push(SuggestedAction {
        id: "mcp-context-route".to_string(),
        title: "Prepare external context route".to_string(),
        description: "Potential MCP branch: pull GitHub issues, docs, or PR context if local evidence is not enough.".to_string(),
        confidence: 0.74,
        action: "mcp_context".to_string(),
    });
    out.push(SuggestedAction {
        id: "workflow-alternative".to_string(),
        title: "Alternative workflow path".to_string(),
        description: "Inspect router telemetry first, then summarize the top evidence path, then produce a grounded answer.".to_string(),
        confidence: 0.77,
        action: "workflow_alternative".to_string(),
    });
    out.push(SuggestedAction {
        id: "create-brief".to_string(),
        title: "Create output brief".to_string(),
        description: "Generate a short answer with source paths and confidence notes.".to_string(),
        confidence: 0.81,
        action: "create_brief".to_string(),
    });
    out
}

fn output_preview(
    query: &str,
    results: &[skygrep_protocol::SearchResult],
    quality: SearchQuality,
) -> OutputPreview {
    let sources = results
        .iter()
        .take(4)
        .map(|result| {
            if let (Some(start), Some(end)) = (result.start_line, result.end_line) {
                format!("{}:{start}-{end}", result.path)
            } else {
                result.path.clone()
            }
        })
        .collect::<Vec<_>>();
    let readiness = match quality {
        SearchQuality::Best => "Ready",
        SearchQuality::Degraded => "Needs review",
        SearchQuality::Uncertain => "Uncertain",
    }
    .to_string();
    OutputPreview {
        title: "Local Search Brief".to_string(),
        summary: if results.is_empty() {
            format!("No confident local evidence was found for \"{query}\".")
        } else if let Some(top) = results.first() {
            format!(
                "For \"{query}\", the strongest evidence is {} with score {:.0}%. {}",
                result_source(top),
                top.score * 100.0,
                top.snippet.replace('\n', " ")
            )
        } else {
            format!(
                "Found {} ranked local source(s) for \"{query}\". The preview is grounded in the top results.",
                results.len()
            )
        },
        readiness,
        sources,
    }
}

fn result_source(result: &skygrep_protocol::SearchResult) -> String {
    if let (Some(start), Some(end)) = (result.start_line, result.end_line) {
        format!("{}:{start}-{end}", result.path)
    } else if let Some(start) = result.start_line {
        format!("{}:{start}-{start}", result.path)
    } else {
        result.path.clone()
    }
}

fn fallback_gap(results: &[skygrep_protocol::SearchResult]) -> f64 {
    match (results.first(), results.get(1)) {
        (Some(first), Some(second)) => (first.score - second.score).max(0.0),
        (Some(first), None) => first.score,
        _ => 0.0,
    }
}

fn fallback_results(root: &Path, query: &str, top_k: usize) -> Vec<skygrep_protocol::SearchResult> {
    let terms = query_terms(query);
    if terms.is_empty() {
        return fallback_seed_results(root, top_k);
    }
    let mut out = Vec::new();
    collect_fallback_results(root, root, &terms, &mut out, 0, 1800);
    if out.is_empty() {
        out = fallback_seed_results(root, top_k);
    }
    out.sort_by(|a, b| b.score.total_cmp(&a.score));
    out.truncate(top_k);
    out
}

fn fallback_seed_results(root: &Path, top_k: usize) -> Vec<skygrep_protocol::SearchResult> {
    let seeds = [
        "apps/desktop/src-tauri/src/lib.rs",
        "apps/desktop/src/App.tsx",
        "crates/skygrep-agent/src/lib.rs",
        "crates/skygrep-core/src/search.rs",
        "AGENTS.md",
        "README.md",
    ];
    seeds
        .iter()
        .filter_map(|relative| {
            let path = root.join(relative);
            let content = fs::read_to_string(&path).ok()?;
            let snippet = content
                .lines()
                .filter(|line| !line.trim().is_empty())
                .take(4)
                .collect::<Vec<_>>()
                .join("\n")
                .chars()
                .take(360)
                .collect::<String>();
            Some(skygrep_protocol::SearchResult {
                path: (*relative).to_string(),
                start_line: Some(1),
                end_line: Some(4),
                language: language_for(relative),
                score: 0.38,
                semantic_score: None,
                lexical_score: Some(0.38),
                symbol_boost: None,
                source_type: "local-context-fallback".to_string(),
                snippet,
            })
        })
        .take(top_k)
        .collect()
}

fn collect_fallback_results(
    root: &Path,
    dir: &Path,
    terms: &[String],
    out: &mut Vec<skygrep_protocol::SearchResult>,
    depth: usize,
    max_results: usize,
) {
    if depth > 8 || out.len() > max_results {
        return;
    }
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = path
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or_default();
        if should_skip(name) {
            continue;
        }
        if path.is_dir() {
            collect_fallback_results(root, &path, terms, out, depth + 1, max_results);
            continue;
        }
        if !looks_textual(&path) {
            continue;
        }
        let Ok(content) = fs::read_to_string(&path) else {
            continue;
        };
        let relative = path
            .strip_prefix(root)
            .unwrap_or(path.as_path())
            .to_string_lossy()
            .replace('\\', "/");
        if let Some(result) = score_file(&relative, &content, terms) {
            out.push(result);
        }
    }
}

fn query_terms(query: &str) -> Vec<String> {
    query
        .split(|ch: char| !ch.is_ascii_alphanumeric())
        .map(str::trim)
        .filter(|part| part.len() > 2)
        .map(|part| part.to_ascii_lowercase())
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect()
}

fn score_file(
    relative: &str,
    content: &str,
    terms: &[String],
) -> Option<skygrep_protocol::SearchResult> {
    let haystack = format!("{relative}\n{content}").to_ascii_lowercase();
    let matched = terms
        .iter()
        .filter(|term| haystack.contains(term.as_str()))
        .count();
    if matched == 0 {
        return None;
    }
    let path_bonus = terms
        .iter()
        .filter(|term| relative.to_ascii_lowercase().contains(term.as_str()))
        .count() as f64
        * 0.08;
    let score = ((matched as f64 / terms.len() as f64) * 0.7 + path_bonus + 0.18).min(0.86);
    let (start_line, snippet) = best_snippet(content, terms);
    Some(skygrep_protocol::SearchResult {
        path: relative.to_string(),
        start_line: Some(start_line),
        end_line: Some(start_line + snippet.lines().count().saturating_sub(1) as i64),
        language: language_for(relative),
        score,
        semantic_score: None,
        lexical_score: Some(score),
        symbol_boost: None,
        source_type: "lexical-fallback".to_string(),
        snippet,
    })
}

fn best_snippet(content: &str, terms: &[String]) -> (i64, String) {
    let lines = content.lines().collect::<Vec<_>>();
    let mut best_index = 0usize;
    let mut best_hits = 0usize;
    for (index, line) in lines.iter().enumerate() {
        let lower = line.to_ascii_lowercase();
        let hits = terms
            .iter()
            .filter(|term| lower.contains(term.as_str()))
            .count();
        if hits > best_hits {
            best_hits = hits;
            best_index = index;
        }
    }
    let start = best_index.saturating_sub(1);
    let end = (best_index + 2).min(lines.len().saturating_sub(1));
    let snippet = if lines.is_empty() {
        String::new()
    } else {
        lines[start..=end].join("\n")
    };
    ((start + 1) as i64, snippet.chars().take(360).collect())
}

fn language_for(relative: &str) -> Option<String> {
    PathBuf::from(relative)
        .extension()
        .and_then(|ext| ext.to_str())
        .map(|ext| match ext {
            "rs" => "rust",
            "py" => "python",
            "ts" | "tsx" => "typescript",
            "js" | "jsx" => "javascript",
            "md" => "markdown",
            "toml" => "toml",
            "json" => "json",
            other => other,
        })
        .map(str::to_string)
}

fn looks_textual(path: &Path) -> bool {
    path.extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| {
            matches!(
                ext,
                "rs" | "py"
                    | "ts"
                    | "tsx"
                    | "js"
                    | "jsx"
                    | "md"
                    | "toml"
                    | "json"
                    | "yaml"
                    | "yml"
                    | "txt"
                    | "css"
                    | "html"
            )
        })
}

fn should_skip(name: &str) -> bool {
    matches!(
        name,
        ".git" | "target" | "node_modules" | "dist" | "build" | ".venv" | "__pycache__"
    )
}

fn count_candidate_files(root: &Path) -> usize {
    fn walk(dir: &Path, count: &mut usize, depth: usize) {
        if depth > 8 {
            return;
        }
        let Ok(entries) = fs::read_dir(dir) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = path
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or_default();
            if should_skip(name) {
                continue;
            }
            if path.is_dir() {
                walk(&path, count, depth + 1);
            } else if looks_textual(&path) {
                *count += 1;
            }
        }
    }
    let mut count = 0;
    walk(root, &mut count, 0);
    count
}

#[cfg(test)]
mod tests {
    use super::*;
    use skygrep_core::SearchConfig;
    use std::path::PathBuf;

    #[test]
    fn failed_run_still_emits_terminal_quality() {
        let config = SearchConfig {
            root: PathBuf::from("/tmp/no-such-skygrep-root"),
            db_path: PathBuf::from("/tmp/no-such-skygrep-db/index.db"),
            ollama_url: "http://127.0.0.1:1".to_string(),
            embed_model: "bge-m3".to_string(),
            top_k: 5,
        };
        let events = run_workflow(config, "token savings");
        assert!(matches!(
            events.last(),
            Some(WorkflowEvent::RunCompleted {
                quality: SearchQuality::Uncertain
            })
        ));
    }
}
