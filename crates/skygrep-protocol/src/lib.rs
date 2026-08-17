use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum AgentPhase {
    Search,
    Analyze,
    Synthesize,
    Output,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum SearchQuality {
    Best,
    Degraded,
    Uncertain,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum LogLevel {
    Info,
    Warn,
    Error,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct SearchResult {
    pub path: String,
    pub start_line: Option<i64>,
    pub end_line: Option<i64>,
    pub language: Option<String>,
    pub score: f64,
    pub semantic_score: Option<f64>,
    pub lexical_score: Option<f64>,
    pub symbol_boost: Option<f64>,
    pub source_type: String,
    pub snippet: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct CascadeTelemetry {
    pub path: String,
    pub gap: f64,
    pub tau: f64,
    pub tau_static: f64,
    pub tau_mode: String,
    pub early_exit: bool,
    pub quality: SearchQuality,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct WorkflowNode {
    pub id: String,
    pub label: String,
    pub detail: String,
    pub phase: AgentPhase,
    pub state: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct WorkflowEdge {
    pub from: String,
    pub to: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct WorkflowGraph {
    pub id: String,
    pub nodes: Vec<WorkflowNode>,
    pub edges: Vec<WorkflowEdge>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct SuggestedAction {
    pub id: String,
    pub title: String,
    pub description: String,
    pub confidence: f64,
    pub action: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct OutputPreview {
    pub title: String,
    pub summary: String,
    pub readiness: String,
    pub sources: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "PascalCase")]
pub enum WorkflowEvent {
    CommandSubmitted {
        query: String,
        at: String,
    },
    PhaseChanged {
        phase: AgentPhase,
        progress: f64,
    },
    SearchStarted {
        query: String,
        roots: Vec<String>,
    },
    SearchResultAdded {
        result: SearchResult,
    },
    TelemetryUpdated {
        telemetry: CascadeTelemetry,
        #[serde(rename = "indexedFileCount")]
        indexed_file_count: usize,
        #[serde(rename = "candidateCount")]
        candidate_count: usize,
    },
    WorkflowGraphUpdated {
        graph: WorkflowGraph,
    },
    SuggestionAdded {
        suggestion: SuggestedAction,
    },
    OutputPreviewUpdated {
        output: OutputPreview,
    },
    LogAppended {
        level: LogLevel,
        message: String,
        at: String,
    },
    RunCompleted {
        quality: SearchQuality,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct RunSummary {
    pub query: String,
    pub quality: SearchQuality,
    pub results: Vec<SearchResult>,
    pub telemetry: Option<CascadeTelemetry>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn serializes_internally_tagged_events() {
        let event = WorkflowEvent::PhaseChanged {
            phase: AgentPhase::Analyze,
            progress: 0.5,
        };
        let value = serde_json::to_value(event).unwrap();

        assert_eq!(value["type"], "PhaseChanged");
        assert_eq!(value["phase"], "analyze");
        assert_eq!(value["progress"], 0.5);
    }
}
