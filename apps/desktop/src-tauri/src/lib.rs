use std::{
    env,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::Mutex,
    time::Duration,
};

use serde::{Deserialize, Serialize};
use skygrep_core::resolve_search_config;
use skygrep_protocol::{
    AgentPhase, CascadeTelemetry, LogLevel, OutputPreview, RunSummary, SearchQuality, SearchResult,
    SuggestedAction, WorkflowEdge, WorkflowEvent, WorkflowGraph, WorkflowNode,
};
use tauri::{Emitter, Manager, PhysicalPosition, PhysicalSize, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

const WORKFLOW_EVENT: &str = "skygrep-workflow-event";
const UI_INTERACTION_EVENT: &str = "skygrep-ui-interaction";
const SNAPSHOT_EVENT: &str = "skygrep-desktop-snapshot";
const PANEL_GAP_PX: i32 = 8;

#[derive(Debug, Clone, Copy)]
struct NativePanel {
    label: &'static str,
    surface: &'static str,
    title: &'static str,
    width: f64,
    height: f64,
    x: f64,
    y: f64,
    focusable: bool,
    resizable: bool,
}

#[derive(Debug, Clone, Copy)]
struct LayoutBox {
    x: f64,
    y: f64,
    width: f64,
    height: f64,
}

#[derive(Debug, Clone, Copy)]
struct WindowRect {
    label: &'static str,
    x: i32,
    y: i32,
    width: u32,
    height: u32,
}

impl WindowRect {
    fn right(self) -> i32 {
        self.x + self.width as i32
    }

    fn bottom(self) -> i32 {
        self.y + self.height as i32
    }

    fn overlaps(self, other: Self, gap: i32) -> bool {
        self.x < other.right() + gap
            && self.right() + gap > other.x
            && self.y < other.bottom() + gap
            && self.bottom() + gap > other.y
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct LogEntry {
    level: LogLevel,
    message: String,
    at: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopSnapshot {
    root_path: String,
    query: String,
    phase: AgentPhase,
    progress: f64,
    quality: SearchQuality,
    running: bool,
    results: Vec<SearchResult>,
    graph: WorkflowGraph,
    suggestions: Vec<SuggestedAction>,
    output: OutputPreview,
    selected_result: Option<SearchResult>,
    telemetry: Option<CascadeTelemetry>,
    indexed_file_count: Option<usize>,
    candidate_count: Option<usize>,
    logs: Vec<LogEntry>,
    active_dock: String,
    expanded_suggestions: bool,
    revision: u64,
}

struct DesktopState(Mutex<DesktopSnapshot>);

impl Default for DesktopState {
    fn default() -> Self {
        Self(Mutex::new(default_snapshot()))
    }
}

#[derive(Debug, Deserialize)]
struct CliSearchResult {
    path: String,
    #[serde(default)]
    start_line: Option<i64>,
    #[serde(default)]
    end_line: Option<i64>,
    #[serde(default)]
    language: Option<String>,
    #[serde(default)]
    score: Option<f64>,
    #[serde(default)]
    snippet: Option<String>,
    #[serde(default)]
    source_type: Option<String>,
    #[serde(default, rename = "sourceType")]
    source_type_camel: Option<String>,
    #[serde(default)]
    semantic_score: Option<f64>,
    #[serde(default, rename = "semanticScore")]
    semantic_score_camel: Option<f64>,
    #[serde(default)]
    lexical_score: Option<f64>,
    #[serde(default, rename = "lexicalScore")]
    lexical_score_camel: Option<f64>,
    #[serde(default)]
    symbol_boost: Option<f64>,
    #[serde(default, rename = "symbolBoost")]
    symbol_boost_camel: Option<f64>,
    #[serde(default)]
    fallback: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum AutoRouteKind {
    PathOnly,
    Evidence,
    DeepDetail,
    Answer,
}

#[derive(Debug, Clone)]
struct AutoSkygrepRoute {
    kind: AutoRouteKind,
    label: String,
    reason: String,
    include: Option<String>,
}

impl AutoSkygrepRoute {
    fn json_args(&self, top_k: usize) -> Vec<String> {
        let mut args = vec!["search".to_string(), "--json".to_string()];
        match self.kind {
            AutoRouteKind::PathOnly => {}
            AutoRouteKind::Evidence | AutoRouteKind::DeepDetail | AutoRouteKind::Answer => {
                args.push("--content".to_string());
                args.push("--detail".to_string());
                args.push(if self.kind == AutoRouteKind::DeepDetail {
                    "full".to_string()
                } else {
                    "standard".to_string()
                });
            }
        }
        if let Some(include) = &self.include {
            args.push("--include".to_string());
            args.push(include.clone());
        }
        args.push("--explain".to_string());
        args.push("--top".to_string());
        args.push(top_k.to_string());
        args
    }

    fn display_command(&self, query: &str, top_k: usize) -> String {
        let mut parts = vec!["skygrep".to_string()];
        parts.extend(self.json_args(top_k));
        parts.push(format!("\"{query}\""));
        parts.join(" ")
    }

    fn telemetry_path(&self) -> String {
        match self.kind {
            AutoRouteKind::PathOnly => "auto:path-only-top-k".to_string(),
            AutoRouteKind::Evidence => "auto:standard-evidence".to_string(),
            AutoRouteKind::DeepDetail => "auto:deep-detail-include".to_string(),
            AutoRouteKind::Answer => "auto:answer-synthesis".to_string(),
        }
    }

    fn detail_for_source(source: &str) -> Self {
        Self {
            kind: AutoRouteKind::DeepDetail,
            label: "Deep detail + include".to_string(),
            reason:
                "A concrete evidence path is selected, so the app scopes full detail to that path."
                    .to_string(),
            include: Some(source_path_part(source).to_string()),
        }
    }
}

const NATIVE_PANELS: &[NativePanel] = &[
    NativePanel {
        label: "topbar",
        surface: "topbar",
        title: "Skygrep Top Bar",
        width: 1664.0,
        height: 46.0,
        x: 32.0,
        y: 18.0,
        focusable: true,
        resizable: false,
    },
    NativePanel {
        label: "live-search",
        surface: "live-search",
        title: "Skygrep Live Search",
        width: 500.0,
        height: 588.0,
        x: 72.0,
        y: 78.0,
        focusable: true,
        resizable: true,
    },
    NativePanel {
        label: "logs",
        surface: "logs",
        title: "Skygrep Streaming Logs",
        width: 500.0,
        height: 180.0,
        x: 72.0,
        y: 690.0,
        focusable: true,
        resizable: true,
    },
    NativePanel {
        label: "center-panel",
        surface: "center",
        title: "Skygrep Search Intelligence",
        width: 620.0,
        height: 600.0,
        x: 602.0,
        y: 78.0,
        focusable: true,
        resizable: true,
    },
    NativePanel {
        label: "command",
        surface: "command",
        title: "Skygrep Command",
        width: 620.0,
        height: 176.0,
        x: 602.0,
        y: 690.0,
        focusable: true,
        resizable: true,
    },
    NativePanel {
        label: "agent-status",
        surface: "agent",
        title: "Skygrep Agent Status",
        width: 704.0,
        height: 72.0,
        x: 512.0,
        y: 888.0,
        focusable: false,
        resizable: true,
    },
    NativePanel {
        label: "suggestions",
        surface: "suggestions",
        title: "Skygrep Proactive Actions",
        width: 432.0,
        height: 442.0,
        x: 1244.0,
        y: 420.0,
        focusable: true,
        resizable: true,
    },
    NativePanel {
        label: "output",
        surface: "output",
        title: "Skygrep Output Preview",
        width: 432.0,
        height: 326.0,
        x: 1244.0,
        y: 78.0,
        focusable: true,
        resizable: true,
    },
    NativePanel {
        label: "dock",
        surface: "dock",
        title: "Skygrep Dock",
        width: 248.0,
        height: 72.0,
        x: 34.0,
        y: 888.0,
        focusable: true,
        resizable: true,
    },
];

#[tauri::command]
async fn run_agent(
    app: tauri::AppHandle,
    query: String,
    root: Option<String>,
) -> Result<RunSummary, String> {
    let root = desktop_root(root);
    let config = resolve_search_config(root).map_err(|error| error.to_string())?;
    let start_config = config.clone();
    let query_for_events = query.clone();

    let mut summary = RunSummary {
        query: query.clone(),
        quality: SearchQuality::Uncertain,
        results: Vec::new(),
        telemetry: None,
    };

    for event in skygrep_agent::start_events(&start_config, &query) {
        emit_workflow_event(&app, &mut summary, event).await?;
    }

    let rust_engine_enabled = env::var("SKYGREP_DESKTOP_ENGINE")
        .ok()
        .is_some_and(|value| value.eq_ignore_ascii_case("rust"));
    if !rust_engine_enabled {
        let terminal_config = start_config.clone();
        let terminal_query = query.clone();
        let mut handle = tokio::task::spawn_blocking(move || {
            terminal_search_events(&terminal_config, &terminal_query)
        });
        let mut ticks = 0u16;

        let terminal_events = loop {
            tokio::select! {
                result = &mut handle => {
                    break result.map_err(|error| error.to_string())?;
                }
                _ = tokio::time::sleep(Duration::from_millis(550)) => {
                    ticks += 1;
                    emit_workflow_event(
                        &app,
                        &mut summary,
                        WorkflowEvent::PhaseChanged {
                            phase: AgentPhase::Search,
                            progress: (0.14 + f64::from(ticks.min(18)) * 0.025).min(0.58),
                        },
                    )
                    .await?;
                    if ticks <= 3 || ticks % 10 == 0 {
                        emit_workflow_event(
                            &app,
                            &mut summary,
                            WorkflowEvent::LogAppended {
                                level: LogLevel::Info,
                                message: match ticks {
                                    1 => "Calling current Skygrep terminal router".to_string(),
                                    2 => "Using Skygrep auto route: rg shortcut, index, or semantic cascade".to_string(),
                                    3 => "Waiting for structured JSON evidence from Skygrep".to_string(),
                                    _ => "Skygrep terminal route is still running".to_string(),
                                },
                                at: now(),
                            },
                        )
                        .await?;
                    }
                }
            }
        };

        let terminal_events = terminal_events.unwrap_or_else(|error| {
            skygrep_agent::fallback_events(
                &start_config,
                &query,
                &format!("Skygrep terminal engine unavailable ({error})"),
            )
        });
        for event in terminal_events {
            emit_workflow_event(&app, &mut summary, event).await?;
        }
        return Ok(summary);
    }

    let fallback_config = start_config.clone();
    let fallback_query = query.clone();
    let mut handle = tokio::task::spawn_blocking(move || {
        skygrep_agent::finish_events(config, &query_for_events)
    });
    let mut ticks = 0u8;
    let timeout = tokio::time::sleep(Duration::from_millis(2_500));
    tokio::pin!(timeout);

    let finish_events = loop {
        tokio::select! {
            result = &mut handle => {
                break result.map_err(|error| error.to_string())?;
            }
            _ = &mut timeout => {
                break skygrep_agent::fallback_events(
                    &fallback_config,
                    &fallback_query,
                    "Semantic route exceeded the desktop response budget",
                );
            }
            _ = tokio::time::sleep(Duration::from_millis(550)), if ticks < 8 => {
                ticks += 1;
                let progress = (0.16 + f64::from(ticks) * 0.035).min(0.44);
                emit_workflow_event(
                    &app,
                    &mut summary,
                    WorkflowEvent::PhaseChanged {
                        phase: AgentPhase::Search,
                        progress,
                    },
                )
                .await?;
                emit_workflow_event(
                    &app,
                    &mut summary,
                    WorkflowEvent::LogAppended {
                        level: LogLevel::Info,
                        message: match ticks {
                            1 => "Embedding query and checking semantic index".to_string(),
                            2 => "Ranking candidate files and chunk evidence".to_string(),
                            3 => "Preparing router telemetry for Search Intelligence".to_string(),
                            _ => "Search is still running; keeping panels live".to_string(),
                        },
                        at: now(),
                    },
                )
                .await?;
            }
        }
    };

    for event in finish_events {
        emit_workflow_event(&app, &mut summary, event).await?;
    }

    Ok(summary)
}

#[tauri::command]
async fn run_answer(
    app: tauri::AppHandle,
    query: String,
    root: Option<String>,
) -> Result<OutputPreview, String> {
    let root = desktop_root(root);
    let config = resolve_search_config(root).map_err(|error| error.to_string())?;
    let mut summary = RunSummary {
        query: query.clone(),
        quality: SearchQuality::Uncertain,
        results: Vec::new(),
        telemetry: None,
    };
    emit_workflow_event(
        &app,
        &mut summary,
        WorkflowEvent::PhaseChanged {
            phase: AgentPhase::Synthesize,
            progress: 0.72,
        },
    )
    .await?;
    emit_workflow_event(
        &app,
        &mut summary,
        WorkflowEvent::LogAppended {
            level: LogLevel::Info,
            message: "Calling skygrep --answer with current evidence route".to_string(),
            at: now(),
        },
    )
    .await?;

    let answer_config = config.clone();
    let answer_query = query.clone();
    let preview =
        tokio::task::spawn_blocking(move || terminal_answer_preview(&answer_config, &answer_query))
            .await
            .map_err(|error| error.to_string())??;
    emit_workflow_event(
        &app,
        &mut summary,
        WorkflowEvent::OutputPreviewUpdated {
            output: preview.clone(),
        },
    )
    .await?;
    emit_workflow_event(
        &app,
        &mut summary,
        WorkflowEvent::PhaseChanged {
            phase: AgentPhase::Output,
            progress: 1.0,
        },
    )
    .await?;
    emit_workflow_event(
        &app,
        &mut summary,
        WorkflowEvent::LogAppended {
            level: LogLevel::Info,
            message: "Skygrep answer route complete".to_string(),
            at: now(),
        },
    )
    .await?;
    emit_workflow_event(
        &app,
        &mut summary,
        WorkflowEvent::RunCompleted {
            quality: SearchQuality::Best,
        },
    )
    .await?;
    Ok(preview)
}

#[tauri::command]
async fn run_detail_search(
    app: tauri::AppHandle,
    query: String,
    source: String,
    root: Option<String>,
) -> Result<RunSummary, String> {
    let root = desktop_root(root);
    let config = resolve_search_config(root).map_err(|error| error.to_string())?;
    let detail_config = config.clone();
    let detail_query = query.clone();
    let detail_source = source.clone();
    let mut summary = RunSummary {
        query: query.clone(),
        quality: SearchQuality::Uncertain,
        results: Vec::new(),
        telemetry: None,
    };

    emit_workflow_event(
        &app,
        &mut summary,
        WorkflowEvent::PhaseChanged {
            phase: AgentPhase::Search,
            progress: 0.18,
        },
    )
    .await?;
    emit_workflow_event(
        &app,
        &mut summary,
        WorkflowEvent::LogAppended {
            level: LogLevel::Info,
            message: format!("Calling skygrep --detail full for {source}"),
            at: now(),
        },
    )
    .await?;

    let events = tokio::task::spawn_blocking(move || {
        terminal_detail_events(&detail_config, &detail_query, &detail_source)
    })
    .await
    .map_err(|error| error.to_string())?
    .unwrap_or_else(|error| {
        skygrep_agent::fallback_events(
            &config,
            &query,
            &format!("Skygrep detail route unavailable ({error})"),
        )
    });

    for event in events {
        emit_workflow_event(&app, &mut summary, event).await?;
    }
    Ok(summary)
}

#[tauri::command]
fn get_desktop_snapshot(state: tauri::State<DesktopState>) -> Result<DesktopSnapshot, String> {
    state
        .0
        .lock()
        .map(|snapshot| snapshot.clone())
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn apply_ui_interaction(
    app: tauri::AppHandle,
    state: tauri::State<DesktopState>,
    interaction: serde_json::Value,
) -> Result<DesktopSnapshot, String> {
    let should_restore_layout =
        interaction.get("type").and_then(serde_json::Value::as_str) == Some("ResetWorkspace");
    let snapshot = {
        let mut snapshot = state.0.lock().map_err(|error| error.to_string())?;
        merge_interaction(&mut snapshot, &interaction);
        snapshot.revision += 1;
        snapshot.clone()
    };
    app.emit(SNAPSHOT_EVENT, snapshot.clone())
        .map_err(|error| error.to_string())?;
    app.emit(UI_INTERACTION_EVENT, interaction)
        .map_err(|error| error.to_string())?;
    if should_restore_layout {
        restore_panel_layout(&app)?;
    } else {
        enforce_no_overlap(&app, None)?;
    }
    Ok(snapshot)
}

fn auto_skygrep_route(query: &str) -> AutoSkygrepRoute {
    let normalized = query.to_lowercase();
    let include = extract_query_scope(query);
    let asks_for_location = contains_any(
        &normalized,
        &[
            "where",
            "locate",
            "find",
            "path",
            "file",
            "implementation",
            "defined",
            "definition",
            "which file",
        ],
    ) || contains_any(
        query,
        &[
            "哪里", "在哪", "路径", "路徑", "文件", "实现", "實現", "位置", "找到",
        ],
    );
    let asks_for_content = contains_any(
        &normalized,
        &[
            "what does",
            "what is",
            "show",
            "snippet",
            "content",
            "say about",
            "mentions",
            "evidence",
            "why",
            "explain",
        ],
    ) || contains_any(
        query,
        &[
            "内容",
            "片段",
            "证据",
            "證據",
            "解释",
            "解釋",
            "为什么",
            "為什麼",
            "说明",
            "說明",
        ],
    );
    let asks_for_answer =
        contains_any(
            &normalized,
            &[
                "answer",
                "summarize",
                "summary",
                "brief",
                "write",
                "draft",
                "compose",
                "tell me",
            ],
        ) || contains_any(query, &["总结", "總結", "回答", "摘要", "生成", "写", "寫"]);
    let asks_for_detail = contains_any(
        &normalized,
        &[
            "detail",
            "deep",
            "full",
            "read deeper",
            "entire file",
            "whole file",
        ],
    ) || contains_any(query, &["详细", "詳盡", "完整", "深读", "深讀"]);

    if let Some(scope) = include {
        return AutoSkygrepRoute {
            kind: AutoRouteKind::DeepDetail,
            label: "Deep detail + include".to_string(),
            reason: format!("The query already names {scope}, so Skygrep scopes full-detail extraction instead of searching the whole repo."),
            include: Some(scope),
        };
    }
    if asks_for_answer && !asks_for_location {
        return AutoSkygrepRoute {
            kind: AutoRouteKind::Answer,
            label: "Answer synthesis".to_string(),
            reason: "The query asks for a synthesized local answer, so the app chooses --answer --content automatically.".to_string(),
            include: None,
        };
    }
    if asks_for_detail {
        return AutoSkygrepRoute {
            kind: AutoRouteKind::Evidence,
            label: "Standard evidence first".to_string(),
            reason: "The query asks for detail but no path is known yet, so Skygrep finds the anchor before full-detail scope.".to_string(),
            include: None,
        };
    }
    if asks_for_location && !asks_for_content {
        return AutoSkygrepRoute {
            kind: AutoRouteKind::PathOnly,
            label: "Path lookup top-k".to_string(),
            reason: "The query is looking for a file or implementation location, so snippets are skipped until an anchor is selected.".to_string(),
            include: None,
        };
    }
    AutoSkygrepRoute {
        kind: AutoRouteKind::Evidence,
        label: "Standard evidence".to_string(),
        reason:
            "The query needs local evidence snippets, so Skygrep uses --content --detail standard."
                .to_string(),
        include: None,
    }
}

fn contains_any(haystack: &str, needles: &[&str]) -> bool {
    needles.iter().any(|needle| haystack.contains(needle))
}

fn extract_query_scope(query: &str) -> Option<String> {
    query
        .split_whitespace()
        .map(|token| {
            token.trim_matches(|value: char| {
                matches!(
                    value,
                    '"' | '\'' | '`' | ',' | ';' | ')' | '(' | '[' | ']' | '{' | '}'
                )
            })
        })
        .find(|token| {
            !token.starts_with("http")
                && (token.contains('/')
                    || token.contains('\\')
                    || file_like_extension(token)
                    || token.ends_with("Cargo.toml")
                    || token.ends_with("pyproject.toml"))
        })
        .map(|token| source_path_part(token).replace('\\', "/"))
}

fn file_like_extension(token: &str) -> bool {
    [
        ".rs", ".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".mdx", ".txt", ".toml", ".json",
        ".yaml", ".yml", ".html", ".css", ".pdf", ".docx",
    ]
    .iter()
    .any(|extension| token.ends_with(extension))
}

fn terminal_search_events(
    config: &skygrep_core::SearchConfig,
    query: &str,
) -> Result<Vec<WorkflowEvent>, String> {
    let route = auto_skygrep_route(query);
    if route.kind == AutoRouteKind::Answer {
        return terminal_answer_events(config, query, &route);
    }

    let args = route.json_args(config.top_k);
    let output = Command::new("skygrep")
        .args(&args)
        .arg(query)
        .current_dir(&config.root)
        .output()
        .map_err(|error| format!("failed to launch skygrep: {error}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "skygrep exited with status {}: {}",
            output.status,
            stderr.trim()
        ));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let cli_results: Vec<CliSearchResult> = serde_json::from_str(&stdout)
        .map_err(|error| format!("failed to decode skygrep JSON: {error}"))?;
    let results = cli_results
        .into_iter()
        .map(|result| cli_result_to_search_result(config, result))
        .collect::<Vec<_>>();
    Ok(terminal_events_from_results(config, query, results, &route))
}

fn terminal_detail_events(
    config: &skygrep_core::SearchConfig,
    query: &str,
    source: &str,
) -> Result<Vec<WorkflowEvent>, String> {
    let route = AutoSkygrepRoute::detail_for_source(source);
    let include = route.include.as_deref().unwrap_or(source_path_part(source));
    let output = Command::new("skygrep")
        .args(route.json_args(config.top_k))
        .arg(query)
        .current_dir(&config.root)
        .output()
        .map_err(|error| format!("failed to launch skygrep detail route: {error}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "skygrep detail exited with status {}: {}",
            output.status,
            stderr.trim()
        ));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let cli_results: Vec<CliSearchResult> = serde_json::from_str(&stdout)
        .map_err(|error| format!("failed to decode skygrep detail JSON: {error}"))?;
    let results = cli_results
        .into_iter()
        .map(|result| cli_result_to_search_result(config, result))
        .collect::<Vec<_>>();
    let mut events = vec![WorkflowEvent::LogAppended {
        level: LogLevel::Info,
        message: format!("Detail route scoped to {include}"),
        at: now(),
    }];
    events.extend(terminal_events_from_results(config, query, results, &route));
    Ok(events)
}

fn terminal_events_from_results(
    config: &skygrep_core::SearchConfig,
    query: &str,
    results: Vec<SearchResult>,
    route: &AutoSkygrepRoute,
) -> Vec<WorkflowEvent> {
    let mut events = Vec::new();
    let quality = terminal_quality(&results);
    let gap = match (results.first(), results.get(1)) {
        (Some(first), Some(second)) => (first.score - second.score).max(0.0),
        (Some(first), None) => first.score,
        _ => 0.0,
    };
    events.push(WorkflowEvent::LogAppended {
        level: LogLevel::Info,
        message: format!("Auto option selected: {} ({})", route.label, route.reason),
        at: now(),
    });
    events.push(WorkflowEvent::LogAppended {
        level: LogLevel::Info,
        message: route.display_command(query, config.top_k),
        at: now(),
    });
    events.push(WorkflowEvent::LogAppended {
        level: LogLevel::Info,
        message: format!(
            "Skygrep terminal router returned {} structured result(s)",
            results.len()
        ),
        at: now(),
    });
    events.push(WorkflowEvent::TelemetryUpdated {
        telemetry: CascadeTelemetry {
            path: route.telemetry_path(),
            gap,
            tau: 0.015,
            tau_static: 0.015,
            tau_mode: "auto-option".to_string(),
            early_exit: false,
            quality: quality.clone(),
        },
        indexed_file_count: count_visible_files(&config.root),
        candidate_count: results.len(),
    });
    for result in &results {
        events.push(WorkflowEvent::SearchResultAdded {
            result: result.clone(),
        });
    }
    events.push(WorkflowEvent::WorkflowGraphUpdated {
        graph: default_graph_for_run("done", "running", "pending", "pending"),
    });
    events.push(WorkflowEvent::PhaseChanged {
        phase: AgentPhase::Analyze,
        progress: 0.58,
    });
    events.push(WorkflowEvent::LogAppended {
        level: LogLevel::Info,
        message: "Analyzing Skygrep evidence paths for desktop preview".to_string(),
        at: now(),
    });
    for suggestion in terminal_suggestions(query, &results, &quality) {
        events.push(WorkflowEvent::SuggestionAdded { suggestion });
    }
    events.push(WorkflowEvent::WorkflowGraphUpdated {
        graph: default_graph_for_run("done", "done", "running", "pending"),
    });
    events.push(WorkflowEvent::PhaseChanged {
        phase: AgentPhase::Synthesize,
        progress: 0.82,
    });
    events.push(WorkflowEvent::OutputPreviewUpdated {
        output: terminal_output_preview(query, &results, &quality),
    });
    events.push(WorkflowEvent::WorkflowGraphUpdated {
        graph: default_graph_for_run("done", "done", "done", "done"),
    });
    events.push(WorkflowEvent::PhaseChanged {
        phase: AgentPhase::Output,
        progress: 1.0,
    });
    events.push(WorkflowEvent::LogAppended {
        level: LogLevel::Info,
        message: "Skygrep terminal route complete".to_string(),
        at: now(),
    });
    events.push(WorkflowEvent::RunCompleted { quality });
    events
}

fn terminal_answer_events(
    config: &skygrep_core::SearchConfig,
    query: &str,
    route: &AutoSkygrepRoute,
) -> Result<Vec<WorkflowEvent>, String> {
    let output = terminal_answer_preview(config, query)?;
    let results = answer_results(&output);
    let quality = if output.sources.is_empty() {
        SearchQuality::Uncertain
    } else {
        SearchQuality::Best
    };
    let mut events = vec![
        WorkflowEvent::LogAppended {
            level: LogLevel::Info,
            message: format!("Auto option selected: {} ({})", route.label, route.reason),
            at: now(),
        },
        WorkflowEvent::LogAppended {
            level: LogLevel::Info,
            message: format!(
                "skygrep search --answer --content --top {} \"{}\"",
                config.top_k, query
            ),
            at: now(),
        },
        WorkflowEvent::TelemetryUpdated {
            telemetry: CascadeTelemetry {
                path: route.telemetry_path(),
                gap: results.first().map_or(0.0, |result| result.score),
                tau: 0.015,
                tau_static: 0.015,
                tau_mode: "auto-option".to_string(),
                early_exit: false,
                quality: quality.clone(),
            },
            indexed_file_count: count_visible_files(&config.root),
            candidate_count: results.len(),
        },
    ];
    for result in &results {
        events.push(WorkflowEvent::SearchResultAdded {
            result: result.clone(),
        });
    }
    events.push(WorkflowEvent::WorkflowGraphUpdated {
        graph: default_graph_for_run("done", "done", "running", "pending"),
    });
    events.push(WorkflowEvent::PhaseChanged {
        phase: AgentPhase::Synthesize,
        progress: 0.84,
    });
    for suggestion in terminal_suggestions(query, &results, &quality) {
        events.push(WorkflowEvent::SuggestionAdded { suggestion });
    }
    events.push(WorkflowEvent::OutputPreviewUpdated { output });
    events.push(WorkflowEvent::WorkflowGraphUpdated {
        graph: default_graph_for_run("done", "done", "done", "done"),
    });
    events.push(WorkflowEvent::PhaseChanged {
        phase: AgentPhase::Output,
        progress: 1.0,
    });
    events.push(WorkflowEvent::LogAppended {
        level: LogLevel::Info,
        message: "Skygrep answer synthesis route complete".to_string(),
        at: now(),
    });
    events.push(WorkflowEvent::RunCompleted { quality });
    Ok(events)
}

fn answer_results(output: &OutputPreview) -> Vec<SearchResult> {
    output
        .sources
        .iter()
        .enumerate()
        .map(|(index, source)| {
            let path_part = source_path_part(source).to_string();
            SearchResult {
                path: path_part,
                start_line: source_line(source),
                end_line: source_line(source),
                language: None,
                score: (0.82_f64 - index as f64 * 0.04).max(0.58),
                semantic_score: None,
                lexical_score: None,
                symbol_boost: None,
                source_type: "answer-source".to_string(),
                snippet:
                    "Source selected by skygrep --answer --content for the synthesized answer."
                        .to_string(),
            }
        })
        .collect()
}

fn cli_result_to_search_result(
    config: &skygrep_core::SearchConfig,
    result: CliSearchResult,
) -> SearchResult {
    let path = PathBuf::from(&result.path);
    let display_path = if path.is_absolute() {
        path.strip_prefix(&config.root)
            .unwrap_or(path.as_path())
            .to_string_lossy()
            .replace('\\', "/")
    } else {
        result.path.replace('\\', "/")
    };
    let source_type = result
        .source_type
        .or(result.source_type_camel)
        .or(result.fallback)
        .unwrap_or_else(|| "skygrep-terminal".to_string());
    SearchResult {
        path: display_path,
        start_line: result.start_line,
        end_line: result.end_line,
        language: result.language,
        score: result.score.unwrap_or(0.0).clamp(0.0, 1.0),
        semantic_score: result.semantic_score.or(result.semantic_score_camel),
        lexical_score: result.lexical_score.or(result.lexical_score_camel),
        symbol_boost: result.symbol_boost.or(result.symbol_boost_camel),
        source_type,
        snippet: result.snippet.unwrap_or_default(),
    }
}

fn terminal_quality(results: &[SearchResult]) -> SearchQuality {
    match results.first().map(|result| result.score) {
        Some(score) if score >= 0.55 => SearchQuality::Best,
        Some(_) => SearchQuality::Degraded,
        None => SearchQuality::Uncertain,
    }
}

fn terminal_suggestions(
    query: &str,
    results: &[SearchResult],
    quality: &SearchQuality,
) -> Vec<SuggestedAction> {
    let mut suggestions = Vec::new();
    suggestions.push(SuggestedAction {
        id: "proactive-followup".to_string(),
        title: "Run proactive follow-up".to_string(),
        description: format!(
            "Use the current result set to ask the next narrower search for: {query}"
        ),
        confidence: if *quality == SearchQuality::Uncertain {
            0.62
        } else {
            0.78
        },
        action: "proactive_search".to_string(),
    });
    if let Some(top) = results.first() {
        suggestions.push(SuggestedAction {
            id: "attach-evidence".to_string(),
            title: "Attach top evidence paths".to_string(),
            description: format!(
                "Bind {} ranked path(s) from the SkyGrab route into the output preview.",
                results.len().min(5)
            ),
            confidence: top.score.clamp(0.0, 1.0),
            action: "attach_evidence".to_string(),
        });
        suggestions.push(SuggestedAction {
            id: "explain-router".to_string(),
            title: "Explain SkyGrab route".to_string(),
            description:
                "Show intent, diversion lane, score gap, tau, source type, and uncertainty state."
                    .to_string(),
            confidence: 0.88,
            action: "explain_router".to_string(),
        });
        suggestions.push(SuggestedAction {
            id: "open-top-result".to_string(),
            title: "Open top result".to_string(),
            description: format!(
                "Inspect {} from the Skygrep terminal result.",
                result_source(top)
            ),
            confidence: top.score.clamp(0.0, 1.0),
            action: "open_result".to_string(),
        });
        suggestions.push(SuggestedAction {
            id: "detail-selected".to_string(),
            title: "Request detail from selected path".to_string(),
            description:
                "Prepare the exact focused --detail command once SkyGrab has narrowed the path."
                    .to_string(),
            confidence: 0.84,
            action: "detail_selected".to_string(),
        });
    }
    suggestions.push(SuggestedAction {
        id: "mcp-context-route".to_string(),
        title: "Prepare MCP context route".to_string(),
        description:
            "Future branch: add GitHub/docs/Linear context when local repo evidence is not enough."
                .to_string(),
        confidence: 0.71,
        action: "mcp_context".to_string(),
    });
    suggestions.push(SuggestedAction {
        id: "create-brief".to_string(),
        title: "Create grounded brief".to_string(),
        description: "Write the answer from the visible Skygrep-ranked evidence paths.".to_string(),
        confidence: 0.82,
        action: "create_brief".to_string(),
    });
    suggestions
}

fn terminal_output_preview(
    query: &str,
    results: &[SearchResult],
    quality: &SearchQuality,
) -> OutputPreview {
    let sources = result_sources(results, 5);
    let readiness = match quality {
        SearchQuality::Best => "Ready",
        SearchQuality::Degraded => "Needs review",
        SearchQuality::Uncertain => "Uncertain",
    };
    let summary = if let Some(top) = results.first() {
        format!(
            "Skygrep terminal result for \"{query}\": strongest path is {} at {}. {}",
            result_source(top),
            format_score(top.score),
            top.snippet.replace('\n', " ")
        )
    } else {
        format!("Skygrep terminal route did not return local evidence for \"{query}\".")
    };
    OutputPreview {
        title: "Skygrep Terminal Answer".to_string(),
        summary,
        readiness: readiness.to_string(),
        sources,
    }
}

fn terminal_answer_preview(
    config: &skygrep_core::SearchConfig,
    query: &str,
) -> Result<OutputPreview, String> {
    let output = Command::new("skygrep")
        .arg("search")
        .arg("--answer")
        .arg("--content")
        .arg("--top")
        .arg(config.top_k.to_string())
        .arg(query)
        .current_dir(&config.root)
        .output()
        .map_err(|error| format!("failed to launch skygrep answer route: {error}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "skygrep answer exited with status {}: {}",
            output.status,
            stderr.trim()
        ));
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let sources = answer_sources(config, &stdout);
    let answer = stdout
        .lines()
        .filter(|line| is_answer_body_line(line))
        .collect::<Vec<_>>()
        .join("\n")
        .trim()
        .to_string();
    Ok(OutputPreview {
        title: "Skygrep Terminal Answer".to_string(),
        summary: if answer.is_empty() {
            format!("Skygrep answer route completed for \"{query}\" but did not return a body.")
        } else {
            answer
        },
        readiness: if sources.is_empty() {
            "Needs review".to_string()
        } else {
            "Ready".to_string()
        },
        sources,
    })
}

fn answer_sources(config: &skygrep_core::SearchConfig, stdout: &str) -> Vec<String> {
    stdout
        .lines()
        .filter_map(|line| {
            let trimmed = line.trim();
            if !trimmed.starts_with('/') {
                return None;
            }
            let token = trimmed.split_whitespace().next()?;
            let (path_part, range_part) = token.rsplit_once(':').unwrap_or((token, ""));
            let path = PathBuf::from(path_part);
            let display = path
                .strip_prefix(&config.root)
                .unwrap_or(path.as_path())
                .to_string_lossy()
                .replace('\\', "/");
            if range_part.is_empty() {
                Some(display)
            } else {
                Some(format!("{display}:{range_part}"))
            }
        })
        .take(6)
        .collect()
}

fn is_answer_body_line(line: &&str) -> bool {
    let trimmed = line.trim();
    !trimmed.is_empty()
        && trimmed != "Sources:"
        && !trimmed.starts_with("├")
        && !trimmed.starts_with("└")
        && !trimmed.starts_with("│")
        && !trimmed.starts_with("[Answer completed")
        && !trimmed.starts_with('/')
}

fn count_visible_files(root: &Path) -> usize {
    fn walk(dir: &Path, count: &mut usize, depth: usize) {
        if depth > 8 || *count > 20_000 {
            return;
        }
        let Ok(entries) = std::fs::read_dir(dir) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = path
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or_default();
            if matches!(
                name,
                ".git" | "target" | "node_modules" | "dist" | "build" | ".venv" | "__pycache__"
            ) {
                continue;
            }
            if path.is_dir() {
                walk(&path, count, depth + 1);
            } else {
                *count += 1;
            }
        }
    }

    let mut count = 0;
    walk(root, &mut count, 0);
    count
}

async fn emit_workflow_event(
    app: &tauri::AppHandle,
    summary: &mut RunSummary,
    event: WorkflowEvent,
) -> Result<(), String> {
    update_summary(summary, &event);
    let snapshot = {
        let state = app.state::<DesktopState>();
        let mut snapshot = state.0.lock().map_err(|error| error.to_string())?;
        merge_workflow_event(&mut snapshot, &event);
        snapshot.revision += 1;
        snapshot.clone()
    };
    app.emit(WORKFLOW_EVENT, event)
        .map_err(|error| error.to_string())?;
    app.emit(SNAPSHOT_EVENT, snapshot)
        .map_err(|error| error.to_string())?;
    tokio::time::sleep(Duration::from_millis(35)).await;
    Ok(())
}

#[tauri::command]
async fn show_overlay(window: tauri::Window) -> Result<(), String> {
    window.show().map_err(|error| error.to_string())?;
    window.set_focus().map_err(|error| error.to_string())?;
    Ok(())
}

#[tauri::command]
async fn show_native_panels(app: tauri::AppHandle) -> Result<(), String> {
    set_native_panel_visibility(&app, true).map_err(|error| error.to_string())
}

#[tauri::command]
async fn hide_native_panels(app: tauri::AppHandle) -> Result<(), String> {
    set_native_panel_visibility(&app, false).map_err(|error| error.to_string())
}

#[tauri::command]
fn broadcast_ui_interaction(
    app: tauri::AppHandle,
    interaction: serde_json::Value,
) -> Result<(), String> {
    app.emit(UI_INTERACTION_EVENT, interaction)
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn copy_text(text: String) -> Result<(), String> {
    let mut child = Command::new("pbcopy")
        .stdin(Stdio::piped())
        .spawn()
        .map_err(|error| error.to_string())?;
    let stdin = child
        .stdin
        .as_mut()
        .ok_or_else(|| "pbcopy stdin unavailable".to_string())?;
    stdin
        .write_all(text.as_bytes())
        .map_err(|error| error.to_string())?;
    child.wait().map_err(|error| error.to_string())?;
    Ok(())
}

#[tauri::command]
async fn open_source(source: String, root: Option<String>) -> Result<(), String> {
    let path_part = source_path_part(&source);
    let base = desktop_root(root)
        .or_else(|| env::current_dir().ok())
        .ok_or_else(|| "could not resolve project root".to_string())?;
    let path = PathBuf::from(path_part);
    let candidate = if path.is_absolute() {
        path
    } else {
        base.join(path)
    };
    if !candidate.exists() {
        return Err(format!(
            "source path does not exist: {}",
            candidate.display()
        ));
    }
    Command::new("open")
        .arg(candidate)
        .spawn()
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn source_path_part(source: &str) -> &str {
    source
        .split_once(':')
        .map(|(path, _)| path)
        .unwrap_or(source)
}

fn source_line(source: &str) -> Option<i64> {
    let (_, line_part) = source.rsplit_once(':')?;
    let first_line = line_part.split('-').next().unwrap_or(line_part);
    first_line.parse::<i64>().ok()
}

fn update_summary(summary: &mut RunSummary, event: &WorkflowEvent) {
    match event {
        WorkflowEvent::SearchResultAdded { result } => summary.results.push(result.clone()),
        WorkflowEvent::RunCompleted { quality } => summary.quality = quality.clone(),
        WorkflowEvent::TelemetryUpdated { telemetry, .. } => {
            summary.telemetry = Some(telemetry.clone());
        }
        _ => {}
    }
}

fn merge_workflow_event(snapshot: &mut DesktopSnapshot, event: &WorkflowEvent) {
    match event {
        WorkflowEvent::CommandSubmitted { query, at } => {
            snapshot.query = query.clone();
            snapshot.phase = AgentPhase::Search;
            snapshot.progress = 0.08;
            snapshot.quality = SearchQuality::Best;
            snapshot.running = true;
            snapshot.results.clear();
            snapshot.suggestions.clear();
            snapshot.selected_result = None;
            snapshot.telemetry = None;
            snapshot.indexed_file_count = None;
            snapshot.candidate_count = None;
            snapshot.output = preview_for_submitted_query(query);
            snapshot.logs = vec![LogEntry {
                level: LogLevel::Info,
                message: format!("Command submitted: {query}"),
                at: at.clone(),
            }];
        }
        WorkflowEvent::PhaseChanged { phase, progress } => {
            snapshot.phase = phase.clone();
            snapshot.progress = *progress;
        }
        WorkflowEvent::SearchStarted { roots, .. } => {
            if let Some(root) = roots.first() {
                snapshot.root_path = root.clone();
            }
            push_log(
                snapshot,
                LogLevel::Info,
                format!(
                    "Searching {}",
                    roots.first().map_or("local repo", String::as_str)
                ),
            );
        }
        WorkflowEvent::SearchResultAdded { result } => {
            snapshot.results.push(result.clone());
            if snapshot.selected_result.is_none() {
                snapshot.selected_result = Some(result.clone());
            }
            if snapshot.output.readiness == "Searching" || snapshot.output.sources.is_empty() {
                snapshot.output = preview_for_result(result);
            }
        }
        WorkflowEvent::TelemetryUpdated {
            telemetry,
            indexed_file_count,
            candidate_count,
        } => {
            snapshot.telemetry = Some(telemetry.clone());
            snapshot.indexed_file_count = Some(*indexed_file_count);
            snapshot.candidate_count = Some(*candidate_count);
            snapshot.quality = telemetry.quality.clone();
        }
        WorkflowEvent::WorkflowGraphUpdated { graph } => snapshot.graph = graph.clone(),
        WorkflowEvent::SuggestionAdded { suggestion } => {
            snapshot.suggestions.push(suggestion.clone())
        }
        WorkflowEvent::OutputPreviewUpdated { output } => snapshot.output = output.clone(),
        WorkflowEvent::LogAppended { level, message, at } => {
            push_log_at(snapshot, level.clone(), message.clone(), at.clone());
        }
        WorkflowEvent::RunCompleted { quality } => {
            snapshot.quality = quality.clone();
            snapshot.running = false;
        }
    }
}

fn merge_interaction(snapshot: &mut DesktopSnapshot, interaction: &serde_json::Value) {
    match interaction.get("type").and_then(serde_json::Value::as_str) {
        Some("SelectResult") => {
            if let Some(result) = interaction
                .get("result")
                .and_then(|value| serde_json::from_value::<SearchResult>(value.clone()).ok())
            {
                apply_select_result(snapshot, result);
            }
        }
        Some("RunSuggestion") => {
            if let Some(suggestion) = interaction
                .get("suggestion")
                .and_then(|value| serde_json::from_value::<SuggestedAction>(value.clone()).ok())
            {
                apply_suggestion(snapshot, &suggestion);
            }
        }
        Some("ToggleSuggestions") => {
            snapshot.expanded_suggestions = !snapshot.expanded_suggestions;
            push_log(snapshot, LogLevel::Info, "Toggled expanded suggestions");
        }
        Some("DockSelected") => {
            if let Some(section) = interaction
                .get("section")
                .and_then(serde_json::Value::as_str)
            {
                snapshot.active_dock = section.to_string();
                snapshot.phase = if section == "Command" {
                    AgentPhase::Search
                } else if section == "Workflows" {
                    AgentPhase::Analyze
                } else {
                    AgentPhase::Output
                };
                snapshot.progress = if section == "Command" {
                    0.24
                } else if section == "Workflows" {
                    0.58
                } else {
                    1.0
                };
                snapshot.output = preview_for_dock(section, &result_sources(&snapshot.results, 3));
                push_log(
                    snapshot,
                    LogLevel::Info,
                    format!("Dock section selected: {section}"),
                );
            }
        }
        Some("OpenOutput") => {
            snapshot.phase = AgentPhase::Output;
            snapshot.progress = 1.0;
            push_log(
                snapshot,
                LogLevel::Info,
                "Open requested for current output",
            );
        }
        Some("CopyOutput") => {
            push_log(
                snapshot,
                LogLevel::Info,
                "Copied output preview to clipboard",
            );
        }
        Some("ShareOutput") => {
            snapshot.output.readiness = if snapshot.output.readiness == "Uncertain" {
                "Needs review".to_string()
            } else {
                "Ready".to_string()
            };
            if !snapshot
                .output
                .summary
                .contains("Share package prepared with visible sources")
            {
                snapshot.output.summary = format!(
                    "{} Share package prepared with visible sources and confidence state.",
                    snapshot.output.summary
                );
            }
            push_log(
                snapshot,
                LogLevel::Info,
                "Prepared share package from current preview",
            );
        }
        Some("ExpandOutput") => {
            snapshot.phase = AgentPhase::Output;
            snapshot.progress = 1.0;
            if !snapshot
                .output
                .summary
                .contains("Expanded preview is active")
            {
                snapshot.output.summary = format!(
                    "{} Expanded preview is active; visible sources and router state are preserved across native windows.",
                    snapshot.output.summary
                );
            }
            push_log(snapshot, LogLevel::Info, "Expanded output preview context");
        }
        Some("ResetWorkspace") => {
            snapshot.active_dock = "Command".to_string();
            snapshot.expanded_suggestions = false;
            push_log(
                snapshot,
                LogLevel::Info,
                "Reset workspace layout and dock state",
            );
        }
        _ => {}
    }
}

fn apply_select_result(snapshot: &mut DesktopSnapshot, result: SearchResult) {
    snapshot.selected_result = Some(result.clone());
    snapshot.output = preview_for_result(&result);
    snapshot.phase = AgentPhase::Analyze;
    snapshot.progress = 0.58;
    push_log(
        snapshot,
        LogLevel::Info,
        format!("Selected result {}", result_source(&result)),
    );
}

fn apply_suggestion(snapshot: &mut DesktopSnapshot, suggestion: &SuggestedAction) {
    snapshot.phase = if suggestion.action.contains("brief") {
        AgentPhase::Synthesize
    } else {
        AgentPhase::Analyze
    };
    snapshot.progress = if suggestion.action.contains("brief") {
        0.78
    } else {
        0.62
    };
    push_log(
        snapshot,
        LogLevel::Info,
        format!("Action: {}", suggestion.title),
    );
    let target = snapshot
        .selected_result
        .clone()
        .or_else(|| snapshot.results.first().cloned());

    match suggestion.action.as_str() {
        "open_result" | "open" => {
            if let Some(result) = target {
                snapshot.selected_result = Some(result.clone());
                snapshot.output = preview_for_result(&result);
            }
        }
        "compare" | "compare_candidates" => {
            snapshot.output = OutputPreview {
                title: "Retrieval Comparison".to_string(),
                summary: "Skygrep is showing ranked, bounded context instead of a broad line dump. Use the score and source type to decide whether to inspect or refine.".to_string(),
                readiness: if snapshot.quality == SearchQuality::Uncertain {
                    "Uncertain".to_string()
                } else {
                    "Ready".to_string()
                },
                sources: result_sources(&snapshot.results, 3),
            };
        }
        "proactive_search" => {
            snapshot.output = OutputPreview {
                title: "Proactive Search Candidate".to_string(),
                summary: "Skygrep found a likely explanatory path before the user asks the follow-up: benchmark gap, parity baseline, and token-savings measurement can be inspected as one evidence chain.".to_string(),
                readiness: if snapshot.quality == SearchQuality::Uncertain {
                    "Uncertain".to_string()
                } else {
                    "Ready".to_string()
                },
                sources: result_sources(&snapshot.results, 4),
            };
        }
        "attach_evidence" => {
            let sources = result_sources(&snapshot.results, 5);
            snapshot.output = OutputPreview {
                title: "Attached Evidence Pack".to_string(),
                summary: format!(
                    "SkyGrab auto-attached the strongest local paths for \"{}\". These sources now ground Open, Share, and brief generation actions.",
                    snapshot.query
                ),
                readiness: if sources.is_empty() {
                    "Needs evidence".to_string()
                } else {
                    "Ready".to_string()
                },
                sources,
            };
        }
        "explain_router" => {
            let route = snapshot.telemetry.as_ref().map_or_else(
                || {
                    snapshot.results.first().map_or(
                        "terminal auto diversion is waiting for evidence".to_string(),
                        |result| {
                            format!(
                                "terminal auto diversion inferred from {}",
                                result.source_type
                            )
                        },
                    )
                },
                |telemetry| {
                    format!(
                        "{}; gap {:.4} vs tau {:.4}; mode {}",
                        telemetry.path, telemetry.gap, telemetry.tau, telemetry.tau_mode
                    )
                },
            );
            snapshot.output = OutputPreview {
                title: "SkyGrab Router Trace".to_string(),
                summary: format!(
                    "The app mirrors the terminal route: {route}. Evidence attachment, confidence, and follow-up actions are derived from the same SkyGrab result stream."
                ),
                readiness: if snapshot.quality == SearchQuality::Uncertain {
                    "Needs review".to_string()
                } else {
                    "Ready".to_string()
                },
                sources: result_sources(&snapshot.results, 4),
            };
        }
        "detail_selected" => {
            let source = target.as_ref().map(result_source);
            snapshot.output = OutputPreview {
                title: "Detail Request".to_string(),
                summary: source.as_ref().map_or_else(
                    || "No path is selected yet. Run the terminal route first, then request detail from the strongest evidence path.".to_string(),
                    |path| {
                        let include = path.split_once(':').map_or(path.as_str(), |(path, _)| path);
                        format!(
                            "Next focused command: skygrep --content --detail full --include \"{}\" \"{}\". Use this after SkyGrab has narrowed the path and the user needs the full source context.",
                            include, snapshot.query
                        )
                    },
                ),
                readiness: if source.is_some() {
                    "Ready to run".to_string()
                } else {
                    "Needs evidence".to_string()
                },
                sources: source.into_iter().collect(),
            };
        }
        "workflow_alternative" => {
            snapshot.output = OutputPreview {
                title: "Workflow Alternative".to_string(),
                summary: "Alternative route prepared: inspect parity-vs-ripgrep first, then compare benchmark token budgets, then synthesize the uncertainty notes.".to_string(),
                readiness: "Ready".to_string(),
                sources: result_sources(&snapshot.results, 3),
            };
        }
        "automation_watch" => {
            snapshot.output = OutputPreview {
                title: "Benchmark Drift Watch".to_string(),
                summary: "Automation candidate staged. In a later release this becomes a background watcher over benchmark files and index freshness signals.".to_string(),
                readiness: "Planned".to_string(),
                sources: result_sources(&snapshot.results, 3),
            };
        }
        action if action.starts_with("mcp") => {
            snapshot.output = OutputPreview {
                title: "MCP Workflow Candidate".to_string(),
                summary: "This is a future tool route, not a local search result: the desktop agent can branch into GitHub, Linear, Slack, docs, or browser connectors when local evidence is not enough.".to_string(),
                readiness: "Planned".to_string(),
                sources: result_sources(&snapshot.results, 2),
            };
        }
        "summarize_file" | "brief" | "create_brief" => {
            if let Some(result) = target {
                let telemetry_note = snapshot.telemetry.as_ref().map_or_else(
                    || "visible ranked evidence".to_string(),
                    |telemetry| format!("gap {:.4} vs tau {:.4}", telemetry.gap, telemetry.tau),
                );
                snapshot.output = OutputPreview {
                    title: if suggestion.action == "create_brief" || suggestion.action == "brief" {
                        "Grounded Output Brief".to_string()
                    } else {
                        format!(
                            "Summary: {}",
                            Path::new(&result.path)
                                .file_name()
                                .and_then(|value| value.to_str())
                                .unwrap_or(&result.path)
                        )
                    },
                    summary: format!(
                        "The current answer is grounded in {}. The route is {}; top score is {} with {}.",
                        result.path,
                        snapshot
                            .telemetry
                            .as_ref()
                            .map_or(result.source_type.as_str(), |telemetry| telemetry.path.as_str()),
                        format_score(result.score),
                        telemetry_note
                    ),
                    readiness: if result.score >= 0.7 {
                        "Ready".to_string()
                    } else {
                        "Needs review".to_string()
                    },
                    sources: result_sources(&snapshot.results, 4),
                };
            }
        }
        _ => {
            snapshot.output = OutputPreview {
                title: "Output Brief".to_string(),
                summary: format!(
                    "Prepared a short brief for \"{}\" using {} visible ranked source(s).",
                    snapshot.query,
                    snapshot.results.len()
                ),
                readiness: if snapshot.quality == SearchQuality::Uncertain {
                    "Needs review".to_string()
                } else {
                    "Ready".to_string()
                },
                sources: result_sources(&snapshot.results, 4),
            };
        }
    }
}

fn push_log(snapshot: &mut DesktopSnapshot, level: LogLevel, message: impl Into<String>) {
    push_log_at(snapshot, level, message.into(), now());
}

fn push_log_at(snapshot: &mut DesktopSnapshot, level: LogLevel, message: String, at: String) {
    snapshot.logs.push(LogEntry { level, message, at });
    let keep_from = snapshot.logs.len().saturating_sub(18);
    if keep_from > 0 {
        snapshot.logs.drain(0..keep_from);
    }
}

fn preview_for_submitted_query(query: &str) -> OutputPreview {
    OutputPreview {
        title: "Searching Local Evidence".to_string(),
        summary: format!("Skygrep is routing \"{query}\" through local search. Results, router telemetry, proactive actions, and this preview will update as events arrive."),
        readiness: "Searching".to_string(),
        sources: Vec::new(),
    }
}

fn preview_for_result(result: &SearchResult) -> OutputPreview {
    OutputPreview {
        title: Path::new(&result.path)
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or(&result.path)
            .to_string(),
        summary: format!(
            "Selected {}. Score {} via {}. {}",
            result_source(result),
            format_score(result.score),
            result.source_type,
            result.snippet
        ),
        readiness: if result.score >= 0.7 {
            "Ready".to_string()
        } else {
            "Uncertain".to_string()
        },
        sources: vec![result_source(result)],
    }
}

fn preview_for_dock(section: &str, sources: &[String]) -> OutputPreview {
    let summary = match section {
        "Command" => "Command mode is the desktop overlay surface: ranked evidence, proactive candidates, and the center command spine stay immediately available.",
        "Workflows" => "Workflow graph is separated from the default search surface. It records agent steps, branch alternatives, tool calls, and acceptance decisions.",
        "Knowledge" => "Knowledge mode will expose indexed repositories, symbol maps, embeddings, stale-index warnings, and retrieval health.",
        "Agents" => "Agents mode will manage background workers, proactive search scopes, model routing, and MCP-enabled task runners.",
        "History" => "History will keep local run summaries, confidence states, uncertainty notes, and reusable output briefs.",
        "Settings" => "Settings will control shortcuts, index roots, Ollama endpoints, layout presets, visual modes, and desktop overlay behavior.",
        _ => "Skygrep Desktop workspace state.",
    };
    OutputPreview {
        title: section.to_string(),
        summary: summary.to_string(),
        readiness: if section == "Command" {
            "Ready".to_string()
        } else {
            "Planned".to_string()
        },
        sources: sources.to_vec(),
    }
}

fn result_source(result: &SearchResult) -> String {
    if let Some(start) = result.start_line {
        format!(
            "{}:{start}-{}",
            result.path,
            result.end_line.unwrap_or(start)
        )
    } else {
        result.path.clone()
    }
}

fn result_sources(results: &[SearchResult], limit: usize) -> Vec<String> {
    results.iter().take(limit).map(result_source).collect()
}

fn format_score(score: f64) -> String {
    format!("{}%", (score * 100.0).round() as i64)
}

fn default_snapshot() -> DesktopSnapshot {
    let results = default_results();
    DesktopSnapshot {
        root_path: home_workspace_root()
            .or_else(|| env::current_dir().ok())
            .map(|path| path.display().to_string())
            .unwrap_or_else(|| "local workspace".to_string()),
        query: "Find where token savings are measured and explain the benchmark".to_string(),
        phase: AgentPhase::Search,
        progress: 0.22,
        quality: SearchQuality::Best,
        running: false,
        selected_result: results.first().cloned(),
        results,
        graph: default_graph(),
        suggestions: default_suggestions(),
        output: default_output(),
        telemetry: None,
        indexed_file_count: None,
        candidate_count: None,
        logs: vec![
            LogEntry {
                level: LogLevel::Info,
                message: "Skygrep Desktop ready".to_string(),
                at: now(),
            },
            LogEntry {
                level: LogLevel::Info,
                message: "Default panels restored".to_string(),
                at: now(),
            },
        ],
        active_dock: "Command".to_string(),
        expanded_suggestions: false,
        revision: 0,
    }
}

fn default_results() -> Vec<SearchResult> {
    vec![
        SearchResult {
            path: "benchmarks/agent_context_benchmark.py".to_string(),
            start_line: Some(1),
            end_line: Some(24),
            language: Some("python".to_string()),
            score: 0.91,
            semantic_score: None,
            lexical_score: None,
            symbol_boost: None,
            source_type: "semantic".to_string(),
            snippet: "Deterministic grep-agent vs skylakegrep-agent benchmark. Compares exact term searches against one semantic top-k retrieval.".to_string(),
        },
        SearchResult {
            path: "benchmarks/parity_vs_ripgrep.py".to_string(),
            start_line: Some(1),
            end_line: Some(15),
            language: Some("python".to_string()),
            score: 0.86,
            semantic_score: None,
            lexical_score: None,
            symbol_boost: None,
            source_type: "graph-adjusted".to_string(),
            snippet: "Real-ripgrep vs skylakegrep agent context benchmark. Shells out to rg and compares context-token usage.".to_string(),
        },
        SearchResult {
            path: "benchmarks/token_savings.py".to_string(),
            start_line: Some(1),
            end_line: Some(12),
            language: Some("python".to_string()),
            score: 0.82,
            semantic_score: None,
            lexical_score: None,
            symbol_boost: None,
            source_type: "symbol".to_string(),
            snippet: "Measures retrieval context compression: how many tokens an LLM receives with skygrep top-k retrieval.".to_string(),
        },
    ]
}

fn default_graph() -> WorkflowGraph {
    default_graph_for_run("done", "running", "pending", "pending")
}

fn default_graph_for_run(
    search: &str,
    analyze: &str,
    synthesize: &str,
    output: &str,
) -> WorkflowGraph {
    WorkflowGraph {
        id: "default".to_string(),
        nodes: vec![
            WorkflowNode {
                id: "interpret".to_string(),
                label: "Recognize Intent".to_string(),
                detail: "local question".to_string(),
                phase: AgentPhase::Search,
                state: search.to_string(),
            },
            WorkflowNode {
                id: "search".to_string(),
                label: "Route Diversion".to_string(),
                detail: "terminal router".to_string(),
                phase: AgentPhase::Search,
                state: search.to_string(),
            },
            WorkflowNode {
                id: "analyze".to_string(),
                label: "Retrieve Evidence".to_string(),
                detail: "ranked paths".to_string(),
                phase: AgentPhase::Analyze,
                state: analyze.to_string(),
            },
            WorkflowNode {
                id: "synthesize".to_string(),
                label: "Attach Context".to_string(),
                detail: "source pack".to_string(),
                phase: AgentPhase::Synthesize,
                state: synthesize.to_string(),
            },
            WorkflowNode {
                id: "output".to_string(),
                label: "Generate Output".to_string(),
                detail: "grounded answer".to_string(),
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

fn default_suggestions() -> Vec<SuggestedAction> {
    vec![
        SuggestedAction {
            id: "proactive-gap".to_string(),
            title: "Trace token-savings benchmark gap".to_string(),
            description: "Proactive search found benchmark files that can explain why Skygrep reduces agent context.".to_string(),
            confidence: 0.91,
            action: "proactive_search".to_string(),
        },
        SuggestedAction {
            id: "attach-evidence".to_string(),
            title: "Attach top evidence paths".to_string(),
            description: "Bind the strongest SkyGrab-ranked paths into Output Preview so Open, Share, and brief generation use the same evidence.".to_string(),
            confidence: 0.9,
            action: "attach_evidence".to_string(),
        },
        SuggestedAction {
            id: "explain-router".to_string(),
            title: "Explain SkyGrab route".to_string(),
            description: "Show recognized intent, diversion lane, score gap, confidence, and why this route was chosen.".to_string(),
            confidence: 0.88,
            action: "explain_router".to_string(),
        },
        SuggestedAction {
            id: "detail-selected".to_string(),
            title: "Request detail from selected path".to_string(),
            description: "Prepare the focused skygrep --content --detail full command after the path has been narrowed.".to_string(),
            confidence: 0.84,
            action: "detail_selected".to_string(),
        },
        SuggestedAction {
            id: "mcp-github".to_string(),
            title: "Prepare GitHub evidence workflow".to_string(),
            description: "Potential MCP route: collect related issues, PRs, and benchmark references before writing the brief.".to_string(),
            confidence: 0.84,
            action: "mcp_github".to_string(),
        },
        SuggestedAction {
            id: "workflow-alt".to_string(),
            title: "Alternative: inspect parity first".to_string(),
            description: "Workflow alternative: start from ripgrep parity tests, then compare semantic retrieval deltas.".to_string(),
            confidence: 0.81,
            action: "workflow_alternative".to_string(),
        },
        SuggestedAction {
            id: "brief".to_string(),
            title: "Create output brief".to_string(),
            description: "Generate a short answer with source paths, uncertainty, and follow-up candidates.".to_string(),
            confidence: 0.78,
            action: "brief".to_string(),
        },
        SuggestedAction {
            id: "watch".to_string(),
            title: "Set benchmark drift watch".to_string(),
            description: "Future automation: watch benchmark files and notify when token-savings numbers change.".to_string(),
            confidence: 0.72,
            action: "automation_watch".to_string(),
        },
    ]
}

fn default_output() -> OutputPreview {
    OutputPreview {
        title: "Attached Evidence Preview".to_string(),
        summary: "The current evidence pack points to benchmark files that compare grep-agent context gathering with Skygrep semantic top-k retrieval. Selecting a result or action rewires this preview immediately.".to_string(),
        readiness: "Ready".to_string(),
        sources: vec![
            "benchmarks/agent_context_benchmark.py:1-24".to_string(),
            "benchmarks/parity_vs_ripgrep.py:1-15".to_string(),
        ],
    }
}

fn desktop_root(root: Option<String>) -> Option<PathBuf> {
    root.and_then(|value| canonical_project_root(PathBuf::from(value)))
        .or_else(|| {
            env::var("SKYGREP_ROOT")
                .ok()
                .and_then(|value| canonical_project_root(PathBuf::from(value)))
        })
        .or_else(compile_time_workspace_root)
        .or_else(home_workspace_root)
        .or_else(|| env::current_dir().ok().and_then(canonical_project_root))
}

fn canonical_project_root(path: PathBuf) -> Option<PathBuf> {
    let path = path.canonicalize().unwrap_or(path);
    if project_markers_exist(&path) {
        Some(path)
    } else {
        None
    }
}

fn project_markers_exist(path: &Path) -> bool {
    path.join("AGENTS.md").exists()
        || path.join("pyproject.toml").exists()
        || path.join("crates/skygrep-core").exists()
}

fn compile_time_workspace_root() -> Option<PathBuf> {
    canonical_project_root(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.."))
}

fn home_workspace_root() -> Option<PathBuf> {
    env::var("HOME").ok().and_then(|home| {
        canonical_project_root(PathBuf::from(home).join("Documents/GitHub/skylakegrep"))
    })
}

fn now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

fn set_native_panel_visibility(app: &tauri::AppHandle, visible: bool) -> tauri::Result<()> {
    #[cfg(target_os = "macos")]
    if visible {
        let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
        let _ = app.show();
    }
    if let Some(main) = app.get_webview_window("main") {
        let _ = main.hide();
    }
    if let Some(backdrop) = app.get_webview_window("backdrop") {
        if visible {
            backdrop.show()?;
            backdrop.set_always_on_top(true)?;
            backdrop.set_visible_on_all_workspaces(true)?;
            let _ = backdrop.set_ignore_cursor_events(true);
        } else {
            backdrop.hide()?;
        }
    }
    for panel in NATIVE_PANELS {
        if let Some(window) = app.get_webview_window(panel.label) {
            if visible {
                window.show()?;
                window.set_always_on_top(true)?;
                window.set_visible_on_all_workspaces(true)?;
            } else {
                window.hide()?;
            }
        }
    }
    if visible {
        let _ = enforce_no_overlap(app, None);
        if let Some(command) = app.get_webview_window("command") {
            let _ = command.set_focus();
        }
    } else {
        #[cfg(target_os = "macos")]
        let _ = app.hide();
    }
    Ok(())
}

#[tauri::command]
fn resolve_panel_geometry(app: tauri::AppHandle, label: String) -> Result<(), String> {
    let panel = NATIVE_PANELS
        .iter()
        .find(|panel| panel.label == label)
        .ok_or_else(|| format!("unknown panel label: {label}"))?;
    if !panel.resizable {
        return Ok(());
    }
    resolve_panel_geometry_for(&app, panel)?;
    enforce_no_overlap(&app, Some(panel.label))
}

#[tauri::command]
fn restore_native_layout(app: tauri::AppHandle) -> Result<(), String> {
    restore_panel_layout(&app)
}

fn resolve_panel_geometry_for(app: &tauri::AppHandle, panel: &NativePanel) -> Result<(), String> {
    let Some(window) = app.get_webview_window(panel.label) else {
        return Ok(());
    };
    let Some(zone) = scaled_panel_zone(app, panel)? else {
        return Ok(());
    };
    let position = window.outer_position().map_err(|error| error.to_string())?;
    let size = window.outer_size().map_err(|error| error.to_string())?;
    let width = size.width.min(zone.width).max(zone.min_width);
    let height = size.height.min(zone.height).max(zone.min_height);
    let x = clamp_i32(
        position.x,
        zone.x,
        zone.x + zone.width as i32 - width as i32,
    );
    let y = clamp_i32(
        position.y,
        zone.y,
        zone.y + zone.height as i32 - height as i32,
    );
    if width != size.width || height != size.height {
        window
            .set_size(PhysicalSize::new(width, height))
            .map_err(|error| error.to_string())?;
    }
    if x != position.x || y != position.y {
        window
            .set_position(PhysicalPosition::new(x, y))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn enforce_no_overlap(app: &tauri::AppHandle, active_label: Option<&str>) -> Result<(), String> {
    for panel in NATIVE_PANELS {
        if panel.resizable {
            resolve_panel_geometry_for(app, panel)?;
        }
    }
    for _ in 0..4 {
        let rects = visible_panel_rects(app)?;
        let mut repaired = false;
        'outer: for (index, first) in rects.iter().enumerate() {
            for second in rects.iter().skip(index + 1) {
                if !first.overlaps(*second, PANEL_GAP_PX) {
                    continue;
                }
                let label = collision_repair_label(*first, *second, active_label);
                if let Some(panel) = panel_by_label(label) {
                    restore_panel_to_default(app, panel)?;
                    repaired = true;
                    break 'outer;
                }
            }
        }
        if !repaired {
            return Ok(());
        }
    }
    Ok(())
}

fn visible_panel_rects(app: &tauri::AppHandle) -> Result<Vec<WindowRect>, String> {
    let mut rects = Vec::new();
    for panel in NATIVE_PANELS {
        let Some(window) = app.get_webview_window(panel.label) else {
            continue;
        };
        if !window.is_visible().unwrap_or(false) {
            continue;
        }
        let position = window.outer_position().map_err(|error| error.to_string())?;
        let size = window.outer_size().map_err(|error| error.to_string())?;
        rects.push(WindowRect {
            label: panel.label,
            x: position.x,
            y: position.y,
            width: size.width,
            height: size.height,
        });
    }
    Ok(rects)
}

fn collision_repair_label(
    first: WindowRect,
    second: WindowRect,
    active_label: Option<&str>,
) -> &'static str {
    if active_label == Some(first.label) {
        return first.label;
    }
    if active_label == Some(second.label) {
        return second.label;
    }
    if panel_repair_priority(first.label) >= panel_repair_priority(second.label) {
        first.label
    } else {
        second.label
    }
}

fn panel_repair_priority(label: &str) -> u8 {
    match label {
        "topbar" => 0,
        "agent-status" | "dock" => 1,
        "command" => 2,
        "center-panel" => 3,
        "live-search" | "output" => 4,
        "logs" | "suggestions" => 5,
        _ => 9,
    }
}

fn panel_by_label(label: &str) -> Option<&'static NativePanel> {
    NATIVE_PANELS.iter().find(|panel| panel.label == label)
}

#[derive(Debug, Clone, Copy)]
struct PhysicalZone {
    x: i32,
    y: i32,
    width: u32,
    height: u32,
    min_width: u32,
    min_height: u32,
}

fn scaled_panel_zone(
    app: &tauri::AppHandle,
    panel: &NativePanel,
) -> Result<Option<PhysicalZone>, String> {
    let Some(zone) = panel_zone(panel.label) else {
        return Ok(None);
    };
    let Some(monitor) = app.primary_monitor().map_err(|error| error.to_string())? else {
        return Ok(None);
    };
    let work_area = monitor.work_area();
    let monitor_scale = monitor.scale_factor().max(1.0);
    let work_x = work_area.position.x as f64 / monitor_scale;
    let work_y = work_area.position.y as f64 / monitor_scale;
    let work_width = work_area.size.width as f64 / monitor_scale;
    let work_height = work_area.size.height as f64 / monitor_scale;
    let design_width = 1728.0_f64.min(work_width);
    let design_height = 972.0_f64.min(work_height);
    let base_x = work_x + ((work_width - design_width) / 2.0).max(0.0);
    let base_y = work_y.min(0.0);
    let scale_x = design_width / 1728.0;
    let scale_y = design_height / 972.0;
    let min_width = if panel.resizable {
        ((panel.width * scale_x * 0.58).max(220.0) * monitor_scale).round() as u32
    } else {
        (panel.width * scale_x * monitor_scale).round() as u32
    };
    let min_height = if panel.resizable {
        ((panel.height * scale_y * 0.55).max(68.0) * monitor_scale).round() as u32
    } else {
        (panel.height * scale_y * monitor_scale).round() as u32
    };
    Ok(Some(PhysicalZone {
        x: ((base_x + zone.x * scale_x) * monitor_scale).round() as i32,
        y: ((base_y + zone.y * scale_y) * monitor_scale).round() as i32,
        width: (zone.width * scale_x * monitor_scale).round() as u32,
        height: (zone.height * scale_y * monitor_scale).round() as u32,
        min_width,
        min_height,
    }))
}

fn restore_panel_layout(app: &tauri::AppHandle) -> Result<(), String> {
    for panel in NATIVE_PANELS {
        restore_panel_to_default(app, panel)?;
    }
    enforce_no_overlap(app, None)
}

fn restore_panel_to_default(app: &tauri::AppHandle, panel: &NativePanel) -> Result<(), String> {
    let Some(window) = app.get_webview_window(panel.label) else {
        return Ok(());
    };
    let Some(rect) = scaled_panel_default(app, panel)? else {
        return Ok(());
    };
    window
        .set_size(PhysicalSize::new(rect.width, rect.height))
        .map_err(|error| error.to_string())?;
    window
        .set_position(PhysicalPosition::new(rect.x, rect.y))
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn scaled_panel_default(
    app: &tauri::AppHandle,
    panel: &NativePanel,
) -> Result<Option<WindowRect>, String> {
    let Some(monitor) = app.primary_monitor().map_err(|error| error.to_string())? else {
        return Ok(None);
    };
    let work_area = monitor.work_area();
    let monitor_scale = monitor.scale_factor().max(1.0);
    let work_x = work_area.position.x as f64 / monitor_scale;
    let work_y = work_area.position.y as f64 / monitor_scale;
    let work_width = work_area.size.width as f64 / monitor_scale;
    let work_height = work_area.size.height as f64 / monitor_scale;
    let design_width = 1728.0_f64.min(work_width);
    let design_height = 972.0_f64.min(work_height);
    let base_x = work_x + ((work_width - design_width) / 2.0).max(0.0);
    let base_y = work_y.min(0.0);
    let scale_x = design_width / 1728.0;
    let scale_y = design_height / 972.0;
    Ok(Some(WindowRect {
        label: panel.label,
        x: ((base_x + panel.x * scale_x) * monitor_scale).round() as i32,
        y: ((base_y + panel.y * scale_y) * monitor_scale).round() as i32,
        width: (panel.width * scale_x * monitor_scale).round() as u32,
        height: (panel.height * scale_y * monitor_scale).round() as u32,
    }))
}

fn panel_zone(label: &str) -> Option<LayoutBox> {
    match label {
        "live-search" => Some(LayoutBox {
            x: 58.0,
            y: 72.0,
            width: 514.0,
            height: 604.0,
        }),
        "logs" => Some(LayoutBox {
            x: 58.0,
            y: 684.0,
            width: 514.0,
            height: 192.0,
        }),
        "center-panel" => Some(LayoutBox {
            x: 596.0,
            y: 72.0,
            width: 628.0,
            height: 606.0,
        }),
        "command" => Some(LayoutBox {
            x: 596.0,
            y: 690.0,
            width: 628.0,
            height: 186.0,
        }),
        "agent-status" => Some(LayoutBox {
            x: 500.0,
            y: 884.0,
            width: 730.0,
            height: 80.0,
        }),
        "output" => Some(LayoutBox {
            x: 1240.0,
            y: 72.0,
            width: 444.0,
            height: 336.0,
        }),
        "suggestions" => Some(LayoutBox {
            x: 1240.0,
            y: 416.0,
            width: 444.0,
            height: 456.0,
        }),
        "dock" => Some(LayoutBox {
            x: 30.0,
            y: 884.0,
            width: 260.0,
            height: 82.0,
        }),
        _ => None,
    }
}

fn clamp_i32(value: i32, min: i32, max: i32) -> i32 {
    if max < min {
        min
    } else {
        value.clamp(min, max)
    }
}

fn toggle_native_panels(app: &tauri::AppHandle) {
    let visible = app
        .get_webview_window("command")
        .and_then(|window| window.is_visible().ok())
        .unwrap_or(false);
    let _ = set_native_panel_visibility(app, !visible);
}

fn spawn_native_panels(app: &tauri::App) -> tauri::Result<()> {
    let (origin_x, origin_y, work_width, work_height) = monitor_geometry(app);
    let design_width = 1728.0_f64.min(work_width);
    let design_height = 972.0_f64.min(work_height);
    let base_x = origin_x + ((work_width - design_width) / 2.0).max(0.0);
    let base_y = origin_y.min(0.0);
    let scale_x = design_width / 1728.0;
    let scale_y = design_height / 972.0;

    let backdrop = WebviewWindowBuilder::new(app, "backdrop", WebviewUrl::App("index.html".into()))
        .title("Skygrep Desktop Backdrop")
        .inner_size(design_width, design_height)
        .min_inner_size(design_width, design_height)
        .position(base_x, base_y)
        .decorations(false)
        .transparent(true)
        .shadow(false)
        .resizable(false)
        .always_on_top(true)
        .visible_on_all_workspaces(true)
        .skip_taskbar(true)
        .focused(false)
        .focusable(false)
        .visible(true)
        .build()?;
    let _ = backdrop.set_ignore_cursor_events(true);
    let _ = backdrop.set_visible_on_all_workspaces(true);
    let _ = backdrop.set_always_on_top(true);

    for panel in NATIVE_PANELS {
        let url = WebviewUrl::App(format!("index.html?surface={}", panel.surface).into());
        let width = panel.width * scale_x;
        let height = panel.height * scale_y;
        let min_width = if panel.resizable {
            (width * 0.58).max(220.0)
        } else {
            width
        };
        let min_height = if panel.resizable {
            (height * 0.55).max(68.0)
        } else {
            height
        };
        let zone = panel_zone(panel.label).unwrap_or(LayoutBox {
            x: panel.x,
            y: panel.y,
            width: panel.width,
            height: panel.height,
        });
        let window = WebviewWindowBuilder::new(app, panel.label, url)
            .title(panel.title)
            .inner_size(width, height)
            .min_inner_size(min_width, min_height)
            .max_inner_size(zone.width * scale_x, zone.height * scale_y)
            .position(base_x + panel.x * scale_x, base_y + panel.y * scale_y)
            .decorations(false)
            .transparent(true)
            .shadow(false)
            .resizable(panel.resizable)
            .always_on_top(true)
            .visible_on_all_workspaces(true)
            .skip_taskbar(true)
            .focused(false)
            .focusable(panel.focusable)
            .visible(true)
            .build()?;
        let _ = window.set_visible_on_all_workspaces(true);
        let _ = window.set_always_on_top(true);
    }
    if let Some(main) = app.get_webview_window("main") {
        let _ = main.hide();
        let _ = main.set_ignore_cursor_events(true);
        let _ = main.set_always_on_top(true);
        let _ = main.set_visible_on_all_workspaces(true);
    }
    Ok(())
}

fn monitor_geometry(app: &tauri::App) -> (f64, f64, f64, f64) {
    if let Ok(Some(monitor)) = app.primary_monitor() {
        let work_area = monitor.work_area();
        let scale = monitor.scale_factor().max(1.0);
        return (
            work_area.position.x as f64 / scale,
            work_area.position.y as f64 / scale,
            work_area.size.width as f64 / scale,
            work_area.size.height as f64 / scale,
        );
    }
    (0.0, 0.0, 1728.0, 972.0)
}

fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    use tauri::{
        menu::{Menu, MenuItem},
        tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    };

    let show = MenuItem::with_id(app, "show", "Show Skygrep Desktop", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;

    TrayIconBuilder::new()
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_native_panels(tray.app_handle());
            }
        })
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => toggle_native_panels(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;

    Ok(())
}

pub fn run() {
    tauri::Builder::default()
        .manage(DesktopState::default())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state() == ShortcutState::Pressed {
                        toggle_native_panels(app);
                    }
                })
                .build(),
        )
        .setup(|app| {
            build_tray(app)?;
            spawn_native_panels(app)?;
            let primary_shortcut =
                Shortcut::new(Some(Modifiers::SUPER | Modifiers::SHIFT), Code::Space);
            let _ = app.global_shortcut().register(primary_shortcut);
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.set_ignore_cursor_events(true);
            }
            if let Some(window) = app.get_webview_window("command") {
                let _ = window.set_focus();
            }
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(Duration::from_millis(650)).await;
                let _ = set_native_panel_visibility(&app_handle, true);
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            run_agent,
            get_desktop_snapshot,
            apply_ui_interaction,
            show_overlay,
            show_native_panels,
            hide_native_panels,
            broadcast_ui_interaction,
            copy_text,
            open_source,
            run_answer,
            run_detail_search,
            resolve_panel_geometry,
            restore_native_layout
        ])
        .run(tauri::generate_context!())
        .expect("error while running Skygrep Desktop");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn default_rect(panel: &NativePanel) -> WindowRect {
        WindowRect {
            label: panel.label,
            x: panel.x.round() as i32,
            y: panel.y.round() as i32,
            width: panel.width.round() as u32,
            height: panel.height.round() as u32,
        }
    }

    fn zone_rect(panel: &NativePanel) -> Option<WindowRect> {
        panel_zone(panel.label).map(|zone| WindowRect {
            label: panel.label,
            x: zone.x.round() as i32,
            y: zone.y.round() as i32,
            width: zone.width.round() as u32,
            height: zone.height.round() as u32,
        })
    }

    fn assert_no_overlap(rects: &[WindowRect]) {
        for (index, first) in rects.iter().enumerate() {
            for second in rects.iter().skip(index + 1) {
                assert!(
                    !first.overlaps(*second, PANEL_GAP_PX),
                    "{} overlaps {} inside the {}px native panel gap",
                    first.label,
                    second.label,
                    PANEL_GAP_PX
                );
            }
        }
    }

    #[test]
    fn native_panel_defaults_do_not_overlap() {
        let rects = NATIVE_PANELS.iter().map(default_rect).collect::<Vec<_>>();
        assert_no_overlap(&rects);
    }

    #[test]
    fn native_panel_resize_zones_do_not_overlap() {
        let rects = NATIVE_PANELS
            .iter()
            .filter_map(zone_rect)
            .collect::<Vec<_>>();
        assert_no_overlap(&rects);
    }

    #[test]
    fn active_panel_is_the_collision_repair_target() {
        let first = WindowRect {
            label: "command",
            x: 0,
            y: 0,
            width: 120,
            height: 80,
        };
        let second = WindowRect {
            label: "agent-status",
            x: 40,
            y: 30,
            width: 120,
            height: 80,
        };
        assert_eq!(
            collision_repair_label(first, second, Some("command")),
            "command"
        );
        assert_eq!(
            collision_repair_label(first, second, Some("agent-status")),
            "agent-status"
        );
    }

    #[test]
    fn auto_route_picks_path_lookup_for_location_questions() {
        let route = auto_skygrep_route("where is token refresh implemented?");
        assert_eq!(route.kind, AutoRouteKind::PathOnly);
        assert!(route.json_args(10).contains(&"--json".to_string()));
        assert!(!route.json_args(10).contains(&"--content".to_string()));
    }

    #[test]
    fn auto_route_picks_deep_detail_for_known_scope() {
        let route = auto_skygrep_route("show deployment steps in docs/migration-plan.md");
        assert_eq!(route.kind, AutoRouteKind::DeepDetail);
        assert_eq!(route.include.as_deref(), Some("docs/migration-plan.md"));
        assert!(route.json_args(10).contains(&"full".to_string()));
    }

    #[test]
    fn auto_route_picks_answer_for_synthesis_questions() {
        let route = auto_skygrep_route("summarize the payment retry policy");
        assert_eq!(route.kind, AutoRouteKind::Answer);
        assert_eq!(route.telemetry_path(), "auto:answer-synthesis");
    }
}
