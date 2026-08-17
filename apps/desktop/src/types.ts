export type AgentPhase = "search" | "analyze" | "synthesize" | "output";
export type SearchQuality = "best" | "degraded" | "uncertain";
export type LogLevel = "info" | "warn" | "error";
export type VisualMode = "focus" | "3d";

export type SearchResult = {
  path: string;
  startLine?: number | null;
  endLine?: number | null;
  language?: string | null;
  score: number;
  semanticScore?: number | null;
  lexicalScore?: number | null;
  symbolBoost?: number | null;
  sourceType: string;
  snippet: string;
};

export type CascadeTelemetry = {
  path: string;
  gap: number;
  tau: number;
  tauStatic: number;
  tauMode: string;
  earlyExit: boolean;
  quality: SearchQuality;
};

export type WorkflowNode = {
  id: string;
  label: string;
  detail: string;
  phase: AgentPhase;
  state: string;
};

export type WorkflowEdge = {
  from: string;
  to: string;
};

export type WorkflowGraph = {
  id: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
};

export type SuggestedAction = {
  id: string;
  title: string;
  description: string;
  confidence: number;
  action: string;
};

export type OutputPreview = {
  title: string;
  summary: string;
  readiness: string;
  sources: string[];
};

export type WorkflowEvent =
  | { type: "CommandSubmitted"; query: string; at: string }
  | { type: "PhaseChanged"; phase: AgentPhase; progress: number }
  | { type: "SearchStarted"; query: string; roots: string[] }
  | { type: "SearchResultAdded"; result: SearchResult }
  | {
      type: "TelemetryUpdated";
      telemetry: CascadeTelemetry;
      indexedFileCount: number;
      candidateCount: number;
    }
  | { type: "WorkflowGraphUpdated"; graph: WorkflowGraph }
  | { type: "SuggestionAdded"; suggestion: SuggestedAction }
  | { type: "OutputPreviewUpdated"; output: OutputPreview }
  | { type: "LogAppended"; level: LogLevel; message: string; at: string }
  | { type: "RunCompleted"; quality: SearchQuality };

export type RunSummary = {
  query: string;
  quality: SearchQuality;
  results: SearchResult[];
  telemetry?: CascadeTelemetry | null;
};

export type LogEntry = {
  level: LogLevel;
  message: string;
  at: string;
};

export type DesktopSnapshot = {
  rootPath: string;
  query: string;
  phase: AgentPhase;
  progress: number;
  quality: SearchQuality;
  running: boolean;
  results: SearchResult[];
  graph: WorkflowGraph;
  suggestions: SuggestedAction[];
  output: OutputPreview;
  selectedResult?: SearchResult | null;
  telemetry?: CascadeTelemetry | null;
  indexedFileCount?: number | null;
  candidateCount?: number | null;
  logs: LogEntry[];
  activeDock: string;
  expandedSuggestions: boolean;
  revision: number;
};

export type PanelId =
  | "liveSearch"
  | "workflow"
  | "suggestions"
  | "output"
  | "logs"
  | "agent";

export type PanelState = Record<PanelId, { visible: boolean; pinned: boolean; order: number }>;
