import type {
  OutputPreview,
  PanelState,
  SearchResult,
  SuggestedAction,
  WorkflowGraph,
} from "./types";

export const defaultQuery =
  "Find where token savings are measured and explain the benchmark";

export const defaultPanels: PanelState = {
  liveSearch: { visible: true, pinned: true, order: 0 },
  workflow: { visible: true, pinned: false, order: 1 },
  suggestions: { visible: true, pinned: false, order: 2 },
  output: { visible: true, pinned: true, order: 3 },
  logs: { visible: true, pinned: false, order: 4 },
  agent: { visible: true, pinned: true, order: 5 },
};

export const defaultResults: SearchResult[] = [
  {
    path: "benchmarks/agent_context_benchmark.py",
    startLine: 1,
    endLine: 24,
    language: "python",
    score: 0.91,
    sourceType: "semantic",
    snippet:
      "Deterministic grep-agent vs skylakegrep-agent benchmark. Compares exact term searches against one semantic top-k retrieval.",
  },
  {
    path: "benchmarks/parity_vs_ripgrep.py",
    startLine: 1,
    endLine: 15,
    language: "python",
    score: 0.86,
    sourceType: "graph-adjusted",
    snippet:
      "Real-ripgrep vs skylakegrep agent context benchmark. Shells out to rg and compares context-token usage.",
  },
  {
    path: "benchmarks/token_savings.py",
    startLine: 1,
    endLine: 12,
    language: "python",
    score: 0.82,
    sourceType: "symbol",
    snippet:
      "Measures retrieval context compression: how many tokens an LLM receives with skygrep top-k retrieval.",
  },
];

export const defaultGraph: WorkflowGraph = {
  id: "default",
  nodes: [
    { id: "interpret", label: "Recognize Intent", detail: "local question", phase: "search", state: "done" },
    { id: "search", label: "Route Diversion", detail: "terminal router", phase: "search", state: "running" },
    { id: "analyze", label: "Retrieve Evidence", detail: "ranked paths", phase: "analyze", state: "pending" },
    { id: "synthesize", label: "Attach Context", detail: "source pack", phase: "synthesize", state: "pending" },
    { id: "output", label: "Generate Output", detail: "grounded answer", phase: "output", state: "pending" },
  ],
  edges: [
    { from: "interpret", to: "search" },
    { from: "search", to: "analyze" },
    { from: "analyze", to: "synthesize" },
    { from: "synthesize", to: "output" },
  ],
};

export const defaultSuggestions: SuggestedAction[] = [
  {
    id: "proactive-gap",
    title: "Trace token-savings benchmark gap",
    description: "Proactive search found benchmark files that can explain why Skygrep reduces agent context.",
    confidence: 0.91,
    action: "proactive_search",
  },
  {
    id: "attach-evidence",
    title: "Attach top evidence paths",
    description: "Bind the strongest SkyGrab-ranked paths into Output Preview so Open, Share, and brief generation use the same evidence.",
    confidence: 0.9,
    action: "attach_evidence",
  },
  {
    id: "explain-router",
    title: "Explain SkyGrab route",
    description: "Show recognized intent, diversion lane, score gap, confidence, and why this route was chosen.",
    confidence: 0.88,
    action: "explain_router",
  },
  {
    id: "detail-selected",
    title: "Request detail from selected path",
    description: "Prepare the focused skygrep --content --detail full command after the path has been narrowed.",
    confidence: 0.84,
    action: "detail_selected",
  },
  {
    id: "mcp-github",
    title: "Prepare GitHub evidence workflow",
    description: "Potential MCP route: collect related issues, PRs, and benchmark references before writing the brief.",
    confidence: 0.84,
    action: "mcp_github",
  },
  {
    id: "workflow-alt",
    title: "Alternative: inspect parity first",
    description: "Workflow alternative: start from ripgrep parity tests, then compare semantic retrieval deltas.",
    confidence: 0.81,
    action: "workflow_alternative",
  },
  {
    id: "brief",
    title: "Create output brief",
    description: "Generate a short answer with source paths, uncertainty, and follow-up candidates.",
    confidence: 0.78,
    action: "brief",
  },
  {
    id: "watch",
    title: "Set benchmark drift watch",
    description: "Future automation: watch benchmark files and notify when token-savings numbers change.",
    confidence: 0.72,
    action: "automation_watch",
  },
];

export const defaultOutput: OutputPreview = {
  title: "Attached Evidence Preview",
  summary:
    "The current evidence pack points to benchmark files that compare grep-agent context gathering with Skygrep semantic top-k retrieval. Selecting a result or action rewires this preview immediately.",
  readiness: "Ready",
  sources: [
    "benchmarks/agent_context_benchmark.py:1-24",
    "benchmarks/parity_vs_ripgrep.py:1-15",
  ],
};
