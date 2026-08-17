import {
  Activity,
  ArrowRight,
  BadgeCheck,
  Bell,
  BookOpen,
  Boxes,
  Braces,
  CircleDot,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Command,
  Eye,
  GitBranch,
  History,
  Layers3,
  Loader2,
  Maximize2,
  Moon,
  Network,
  Pin,
  Radar,
  RotateCcw,
  Search,
  Settings,
  Share2,
  Sparkles,
  Terminal,
  X,
  Zap,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { emit, listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import {
  defaultGraph,
  defaultOutput,
  defaultPanels,
  defaultQuery,
  defaultResults,
  defaultSuggestions,
} from "./data";
import { AgentStatus } from "./components/AgentStatus";
import type {
  AgentPhase,
  CascadeTelemetry,
  DesktopSnapshot,
  LogLevel,
  LogEntry,
  OutputPreview,
  PanelId,
  PanelState,
  RunSummary,
  SearchQuality,
  SearchResult,
  SuggestedAction,
  VisualMode,
  WorkflowEvent,
  WorkflowGraph,
} from "./types";

const phaseOrder: AgentPhase[] = ["search", "analyze", "synthesize", "output"];
const WORKFLOW_EVENT = "skygrep-workflow-event";
const UI_INTERACTION_EVENT = "skygrep-ui-interaction";
const SNAPSHOT_EVENT = "skygrep-desktop-snapshot";
const panelLabels: Record<PanelId, string> = {
  liveSearch: "Live Search",
  workflow: "Search Intelligence",
  suggestions: "Proactive Actions",
  output: "Output Preview",
  logs: "Streaming Logs",
  agent: "Skygrep Agent",
};
const dockLabels = ["Command", "Workflows", "Knowledge", "Agents", "History", "Settings"] as const;
type DockSection = (typeof dockLabels)[number];
type Surface =
  | "shell"
  | "topbar"
  | "live-search"
  | "logs"
  | "center"
  | "command"
  | "agent"
  | "suggestions"
  | "output"
  | "dock";
type ResizeDirection =
  | "East"
  | "North"
  | "NorthEast"
  | "NorthWest"
  | "South"
  | "SouthEast"
  | "SouthWest"
  | "West";

type UiInteraction =
  | { id: string; type: "SelectResult"; result: SearchResult }
  | { id: string; type: "RunSuggestion"; suggestion: SuggestedAction }
  | { id: string; type: "ToggleSuggestions" }
  | { id: string; type: "DockSelected"; section: DockSection }
  | { id: string; type: "ModeChanged"; mode: VisualMode }
  | { id: string; type: "OpenOutput" }
  | { id: string; type: "CopyOutput" }
  | { id: string; type: "ShareOutput" }
  | { id: string; type: "ExpandOutput" }
  | { id: string; type: "ResetWorkspace" };
type UiInteractionInput =
  | { type: "SelectResult"; result: SearchResult }
  | { type: "RunSuggestion"; suggestion: SuggestedAction }
  | { type: "ToggleSuggestions" }
  | { type: "DockSelected"; section: DockSection }
  | { type: "ModeChanged"; mode: VisualMode }
  | { type: "OpenOutput" }
  | { type: "CopyOutput" }
  | { type: "ShareOutput" }
  | { type: "ExpandOutput" }
  | { type: "ResetWorkspace" };

type CommandSignal = {
  intent: string;
  route: string;
  confidence: number;
  attachment: string;
  detailPrompt: string;
  nextStep: string;
};

type AutoOptionSignal = {
  label: string;
  route: string;
  reason: string;
};

const isTauriRuntime = () => "__TAURI_INTERNALS__" in window;

function readPanelState(): PanelState {
  try {
    const raw = localStorage.getItem("skygrep.desktop.panels.v1");
    if (!raw) return defaultPanels;
    return { ...defaultPanels, ...JSON.parse(raw) } as PanelState;
  } catch {
    return defaultPanels;
  }
}

function readMode(): VisualMode {
  return localStorage.getItem("skygrep.desktop.mode") === "3d" ? "3d" : "focus";
}

function readSurface(): Surface {
  const value = new URLSearchParams(window.location.search).get("surface");
  switch (value) {
    case "topbar":
    case "live-search":
    case "logs":
    case "center":
    case "command":
    case "agent":
    case "suggestions":
    case "output":
    case "dock":
      return value;
    default:
      return "shell";
  }
}

function timeLabel(value = new Date()) {
  return value.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function displayLog(log: LogEntry) {
  const date = new Date(log.at);
  return {
    ...log,
    at: Number.isNaN(date.getTime()) ? log.at : timeLabel(date),
  };
}

function isDockSection(value: string): value is DockSection {
  return (dockLabels as readonly string[]).includes(value);
}

function shortRoot(rootPath: string) {
  return rootPath.split("/").filter(Boolean).slice(-2).join("/") || rootPath;
}

function formatScore(score: number) {
  return `${Math.round(score * 100)}%`;
}

function resultSource(result: SearchResult) {
  if (result.startLine) {
    return `${result.path}:${result.startLine}-${result.endLine ?? result.startLine}`;
  }
  return result.path;
}

function previewForResult(result: SearchResult): OutputPreview {
  return {
    title: result.path.split("/").pop() ?? "Selected Result",
    summary: `Selected ${resultSource(result)}. Score ${formatScore(result.score)} via ${result.sourceType}. ${result.snippet}`,
    readiness: result.score >= 0.7 ? "Ready" : "Uncertain",
    sources: [resultSource(result)],
  };
}

function previewForSubmittedQuery(query: string): OutputPreview {
  return {
    title: "Searching Local Evidence",
    summary: `Skygrep is routing "${query}" through local search. Results, router telemetry, proactive actions, and this preview will update as events arrive.`,
    readiness: "Searching",
    sources: [],
  };
}

function previewForDock(section: DockSection, sources: string[]): OutputPreview {
  const copy: Record<DockSection, string> = {
    Command: "Command mode is the desktop overlay surface: ranked evidence, proactive candidates, and the center command spine stay immediately available.",
    Workflows: "Workflow graph is separated from the default search surface. It records agent steps, branch alternatives, tool calls, and acceptance decisions.",
    Knowledge: "Knowledge mode will expose indexed repositories, symbol maps, embeddings, stale-index warnings, and retrieval health.",
    Agents: "Agents mode will manage background workers, proactive search scopes, model routing, and MCP-enabled task runners.",
    History: "History will keep local run summaries, confidence states, uncertainty notes, and reusable output briefs.",
    Settings: "Settings will control shortcuts, index roots, Ollama endpoints, layout presets, visual modes, and desktop overlay behavior.",
  };
  return {
    title: section,
    summary: copy[section],
    readiness: section === "Command" ? "Ready" : "Planned",
    sources,
  };
}

function suggestionKind(suggestion: SuggestedAction) {
  if (suggestion.action.includes("attach")) return "Attach";
  if (suggestion.action.includes("router")) return "Router";
  if (suggestion.action.includes("detail")) return "Detail";
  if (suggestion.action.startsWith("mcp")) return "MCP";
  if (suggestion.action.includes("workflow")) return "Workflow";
  if (suggestion.action.includes("automation")) return "Watch";
  if (suggestion.action.includes("proactive")) return "Proactive";
  return "Action";
}

function inferAutoOption(query: string): AutoOptionSignal {
  const normalized = query.toLowerCase();
  const hasScope = /\b[\w.-]+\/[\w./-]+\b/.test(query)
    || /\.(rs|py|ts|tsx|js|jsx|md|mdx|txt|toml|json|yaml|yml|html|css|pdf|docx)\b/i.test(query);
  const asksForLocation = /\b(where|locate|find|path|file|implementation|defined|definition|which file)\b/.test(normalized)
    || /哪里|在哪|路径|路徑|文件|实现|實現|位置|找到/.test(query);
  const asksForContent = /\b(what does|what is|show|snippet|content|say about|mentions|evidence|why|explain)\b/.test(normalized)
    || /内容|片段|证据|證據|解释|解釋|为什么|為什麼|说明|說明/.test(query);
  const asksForAnswer = /\b(answer|summarize|summary|brief|write|draft|compose|tell me)\b/.test(normalized)
    || /总结|總結|回答|摘要|生成|写|寫/.test(query);
  const asksForDetail = /\b(detail|deep|full|read deeper|entire file|whole file)\b/.test(normalized)
    || /详细|詳盡|完整|深读|深讀/.test(query);

  if (hasScope) {
    return {
      label: "Deep detail + include",
      route: "skygrep --json --content --detail full --include <path>",
      reason: "known path/scope",
    };
  }
  if (asksForAnswer && !asksForLocation) {
    return {
      label: "Answer synthesis",
      route: "skygrep --answer --content",
      reason: "synthesized local answer",
    };
  }
  if (asksForDetail) {
    return {
      label: "Standard evidence first",
      route: "skygrep --json --content --detail standard",
      reason: "needs anchor before full detail",
    };
  }
  if (asksForLocation && !asksForContent) {
    return {
      label: "Path lookup top-k",
      route: "skygrep --json --top 10",
      reason: "location/path question",
    };
  }
  return {
    label: "Standard evidence",
    route: "skygrep --json --content --detail standard",
    reason: "snippet evidence question",
  };
}

function inferCommandSignal({
  query,
  results,
  telemetry,
  quality,
  running,
  attachedCount,
}: {
  query: string;
  results: SearchResult[];
  telemetry: CascadeTelemetry | null;
  quality: SearchQuality;
  running: boolean;
  attachedCount: number;
}): CommandSignal {
  const normalized = query.toLowerCase();
  const asksForFile = /\b(file|path|where|locate|find)\b/.test(normalized)
    || /文件|路径|位置|在哪|哪里|找到|尋找|找/.test(query);
  const asksForExplain = /\b(why|explain|reason|how|detail)\b/.test(normalized)
    || /为什么|為什麼|解释|解釋|原因|细节|細節|怎么|如何/.test(query);
  const asksForAnswer = /\b(answer|summarize|summary|brief|write)\b/.test(normalized)
    || /生成|解释|解釋|說明|说明|总结|總結|回答|摘要/.test(query);
  const intent = asksForFile
    ? "locate evidence"
    : asksForExplain
      ? "explain route"
      : asksForAnswer
        ? "grounded answer"
        : "semantic search";
  const autoOption = inferAutoOption(query);
  const route = telemetry?.path ?? autoOption.route;
  const topScore = results[0]?.score ?? (running ? 0.42 : 0.74);
  const confidence = Math.max(0.08, Math.min(1, telemetry ? 1 - telemetry.tau + telemetry.gap : topScore));
  const attachment = results.length
    ? `${attachedCount || Math.min(results.length, 3)} path${(attachedCount || results.length) === 1 ? "" : "s"} attached`
    : running
      ? "waiting for evidence"
      : "no evidence attached";
  const detailPrompt = results[0]
    ? `Need detail from ${results[0].path.split("/").pop()}`
    : "Need narrower scope";
  const nextStep = quality === "uncertain"
    ? "ask targeted follow-up"
    : results.length
      ? "attach evidence then synthesize"
      : autoOption.reason;
  return { intent, route, confidence, attachment, detailPrompt, nextStep };
}

function quickAction(action: string, title: string, description: string, confidence = 0.82): SuggestedAction {
  return {
    id: `quick-${action}`,
    title,
    description,
    confidence,
    action,
  };
}

function attachedSources(results: SearchResult[], output: OutputPreview) {
  return output.sources.length ? output.sources : results.slice(0, 3).map(resultSource);
}

function tiltWindow(event: React.PointerEvent<HTMLElement>) {
  const target = event.currentTarget;
  const rect = target.getBoundingClientRect();
  const px = (event.clientX - rect.left) / rect.width - 0.5;
  const py = (event.clientY - rect.top) / rect.height - 0.5;
  target.style.setProperty("--panel-rotate-x", `${(-py * 8).toFixed(2)}deg`);
  target.style.setProperty("--panel-rotate-y", `${(px * 10).toFixed(2)}deg`);
}

function resetTilt(event: React.PointerEvent<HTMLElement>) {
  event.currentTarget.style.setProperty("--panel-rotate-x", "0deg");
  event.currentTarget.style.setProperty("--panel-rotate-y", "0deg");
}

function startResize(direction: ResizeDirection, event: React.PointerEvent<HTMLButtonElement>) {
  event.preventDefault();
  event.stopPropagation();
  if (!isTauriRuntime()) return;
  void getCurrentWindow().startResizeDragging(direction);
}

function startWindowDrag(event: React.PointerEvent<HTMLElement>) {
  if (!isTauriRuntime() || event.button !== 0) return;
  event.preventDefault();
  void getCurrentWindow().startDragging();
}

export default function App() {
  const [surface] = useState<Surface>(() => readSurface());
  const [rootPath, setRootPath] = useState("local workspace");
  const [query, setQuery] = useState(defaultQuery);
  const [submittedQuery, setSubmittedQuery] = useState(defaultQuery);
  const [mode, setMode] = useState<VisualMode>(() => readMode());
  const [phase, setPhase] = useState<AgentPhase>("search");
  const [progress, setProgress] = useState(0.22);
  const [quality, setQuality] = useState<SearchQuality>("best");
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState<SearchResult[]>(defaultResults);
  const [graph, setGraph] = useState<WorkflowGraph>(defaultGraph);
  const [suggestions, setSuggestions] = useState<SuggestedAction[]>(defaultSuggestions);
  const [output, setOutput] = useState<OutputPreview>(defaultOutput);
  const [selectedResult, setSelectedResult] = useState<SearchResult | null>(defaultResults[0]);
  const [telemetry, setTelemetry] = useState<CascadeTelemetry | null>(null);
  const [indexedFileCount, setIndexedFileCount] = useState<number | null>(null);
  const [candidateCount, setCandidateCount] = useState<number | null>(null);
  const [logs, setLogs] = useState<Array<{ level: LogLevel; message: string; at: string }>>([
    { level: "info", message: "Skygrep Desktop ready", at: timeLabel() },
    { level: "info", message: "Default panels restored", at: timeLabel() },
  ]);
  const [panels, setPanels] = useState<PanelState>(() => readPanelState());
  const [customizing, setCustomizing] = useState(false);
  const [expandedSuggestions, setExpandedSuggestions] = useState(false);
  const [activeDock, setActiveDock] = useState<DockSection>("Command");
  const seenInteractionIds = useRef<Set<string>>(new Set());
  const lastSnapshotRevision = useRef(-1);

  useEffect(() => {
    document.body.dataset.surface = surface;
  }, [surface]);

  useEffect(() => {
    localStorage.setItem("skygrep.desktop.mode", mode);
  }, [mode]);

  useEffect(() => {
    localStorage.setItem("skygrep.desktop.panels.v1", JSON.stringify(panels));
  }, [panels]);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    let unlisten: (() => void) | undefined;
    listen<WorkflowEvent>(WORKFLOW_EVENT, (event) => applyEvent(event.payload)).then((fn) => {
      unlisten = fn;
    });
    return () => unlisten?.();
  }, []);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    let unlisten: (() => void) | undefined;
    let disposed = false;
    const pull = () => {
      invoke<DesktopSnapshot>("get_desktop_snapshot")
        .then((snapshot) => {
          if (!disposed) syncSnapshot(snapshot);
        })
        .catch(() => {
          // Snapshot polling is the cross-window safety net; event listeners still cover browser/dev mode.
        });
    };
    listen<DesktopSnapshot>(SNAPSHOT_EVENT, (event) => syncSnapshot(event.payload)).then((fn) => {
      unlisten = fn;
    });
    pull();
    const interval = window.setInterval(pull, 300);
    return () => {
      disposed = true;
      window.clearInterval(interval);
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        void hideDesktop();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (!isTauriRuntime() || surface === "shell" || surface === "topbar") return;
    const currentWindow = getCurrentWindow();
    let disposed = false;
    let timer: number | undefined;
    const resolveGeometry = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        if (!disposed) {
          void invoke("resolve_panel_geometry", { label: currentWindow.label });
        }
      }, 24);
    };
    let unlistenMove: (() => void) | undefined;
    let unlistenResize: (() => void) | undefined;
    currentWindow.listen("tauri://move", resolveGeometry).then((fn) => {
      unlistenMove = fn;
    });
    currentWindow.listen("tauri://resize", resolveGeometry).then((fn) => {
      unlistenResize = fn;
    });
    resolveGeometry();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      unlistenMove?.();
      unlistenResize?.();
    };
  }, [surface]);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    let unlisten: (() => void) | undefined;
    listen<UiInteraction>(UI_INTERACTION_EVENT, (event) => {
      if (seenInteractionIds.current.has(event.payload.id)) return;
      seenInteractionIds.current.add(event.payload.id);
      applyInteraction(event.payload);
    }).then((fn) => {
      unlisten = fn;
    });
    return () => unlisten?.();
  }, []);

  const visiblePanels = useMemo(() => {
    return Object.entries(panels)
      .filter(([, value]) => value.visible)
      .sort((a, b) => a[1].order - b[1].order)
      .map(([key]) => key as PanelId);
  }, [panels]);
  const signalQuery = query.trim() || submittedQuery;
  const commandSignal = useMemo(() => inferCommandSignal({
    query: signalQuery,
    results,
    telemetry,
    quality,
    running,
    attachedCount: output.sources.length,
  }), [output.sources.length, quality, results, running, signalQuery, telemetry]);

  const addLog = useCallback((level: LogLevel, message: string, at = timeLabel()) => {
    setLogs((current) => [...current.slice(-14), { level, message, at }]);
  }, []);

  const syncSnapshot = useCallback((snapshot: DesktopSnapshot) => {
    if (snapshot.revision <= lastSnapshotRevision.current) return;
    lastSnapshotRevision.current = snapshot.revision;
    setRootPath(snapshot.rootPath);
    setSubmittedQuery(snapshot.query);
    setPhase(snapshot.phase);
    setProgress(snapshot.progress);
    setQuality(snapshot.quality);
    setRunning(snapshot.running);
    setResults(snapshot.results);
    setGraph(snapshot.graph);
    setSuggestions(snapshot.suggestions);
    setOutput(snapshot.output);
    setSelectedResult(snapshot.selectedResult ?? null);
    setTelemetry(snapshot.telemetry ?? null);
    setIndexedFileCount(snapshot.indexedFileCount ?? null);
    setCandidateCount(snapshot.candidateCount ?? null);
    setLogs(snapshot.logs.map(displayLog));
    if (isDockSection(snapshot.activeDock)) {
      setActiveDock(snapshot.activeDock);
    }
    setExpandedSuggestions(snapshot.expandedSuggestions);
  }, []);

  const applyEvent = useCallback((event: WorkflowEvent) => {
    switch (event.type) {
      case "CommandSubmitted":
        setSubmittedQuery(event.query);
        setResults([]);
        setSelectedResult(null);
        setSuggestions([]);
        setTelemetry(null);
        setIndexedFileCount(null);
        setCandidateCount(null);
        setOutput(previewForSubmittedQuery(event.query));
        setRunning(true);
        setQuality("best");
        setPhase("search");
        setProgress(0.08);
        setLogs([{ level: "info", message: `Command submitted: ${event.query}`, at: timeLabel(new Date(event.at)) }]);
        break;
      case "PhaseChanged":
        setPhase(event.phase);
        setProgress(event.progress);
        break;
      case "SearchStarted":
        addLog("info", `Searching ${event.roots[0] ?? "local repo"}`);
        break;
      case "SearchResultAdded":
        setResults((current) => [...current, event.result]);
        setSelectedResult((current) => current ?? event.result);
        setOutput((current) => (
          current.readiness === "Searching" || current.sources.length === 0
            ? previewForResult(event.result)
            : current
        ));
        break;
      case "TelemetryUpdated":
        setTelemetry(event.telemetry);
        setIndexedFileCount(event.indexedFileCount);
        setCandidateCount(event.candidateCount);
        setQuality(event.telemetry.quality);
        break;
      case "WorkflowGraphUpdated":
        setGraph(event.graph);
        break;
      case "SuggestionAdded":
        setSuggestions((current) => [...current, event.suggestion]);
        break;
      case "OutputPreviewUpdated":
        setOutput(event.output);
        break;
      case "LogAppended":
        addLog(event.level, event.message, timeLabel(new Date(event.at)));
        break;
      case "RunCompleted":
        setQuality(event.quality);
        setRunning(false);
        break;
      default:
        break;
    }
  }, [addLog]);

  const runMock = useCallback(async (nextQuery: string) => {
    const sequence: WorkflowEvent[] = [
      { type: "CommandSubmitted", query: nextQuery, at: new Date().toISOString() },
      { type: "PhaseChanged", phase: "search", progress: 0.16 },
      { type: "SearchStarted", query: nextQuery, roots: ["demo repo"] },
      { type: "WorkflowGraphUpdated", graph: defaultGraph },
      ...defaultResults.map((result) => ({ type: "SearchResultAdded", result }) as WorkflowEvent),
      { type: "PhaseChanged", phase: "analyze", progress: 0.48 },
      ...defaultSuggestions.map((suggestion) => ({ type: "SuggestionAdded", suggestion }) as WorkflowEvent),
      { type: "PhaseChanged", phase: "synthesize", progress: 0.76 },
      { type: "OutputPreviewUpdated", output: defaultOutput },
      { type: "PhaseChanged", phase: "output", progress: 1 },
      { type: "RunCompleted", quality: "best" },
    ];
    for (const event of sequence) {
      applyEvent(event);
      await new Promise((resolve) => window.setTimeout(resolve, 90));
    }
  }, [applyEvent]);

  const submit = useCallback(async (event?: FormEvent) => {
    event?.preventDefault();
    const nextQuery = query.trim();
    if (!nextQuery || running) return;
    setRunning(true);
    setQuality("best");
    if (!isTauriRuntime()) {
      await runMock(nextQuery);
      return;
    }
    try {
      await invoke<RunSummary>("run_agent", { query: nextQuery });
    } catch (error) {
      setRunning(false);
      setQuality("uncertain");
      addLog("error", `Agent command failed: ${String(error)}`);
    }
  }, [addLog, query, runMock, running]);

  const updatePanel = (panel: PanelId, patch: Partial<PanelState[PanelId]>) => {
    setPanels((current) => ({
      ...current,
      [panel]: { ...current[panel], ...patch },
    }));
  };

  const movePanel = (panel: PanelId, delta: number) => {
    setPanels((current) => {
      const next = { ...current };
      next[panel] = { ...next[panel], order: next[panel].order + delta };
      return next;
    });
  };

  function publishInteraction(interaction: UiInteractionInput) {
    const full = {
      ...interaction,
      id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    } as UiInteraction;
    seenInteractionIds.current.add(full.id);
    applyInteraction(full);
    if (isTauriRuntime()) {
      invoke<DesktopSnapshot>("apply_ui_interaction", { interaction: full })
        .then(syncSnapshot)
        .catch(() => invoke("broadcast_ui_interaction", { interaction: full }))
        .catch(() => emit(UI_INTERACTION_EVENT, full))
        .catch(() => {
          addLog("warn", "Native window event bus unavailable");
        });
    }
  }

  function applyInteraction(interaction: UiInteraction) {
    switch (interaction.type) {
      case "SelectResult":
        applySelectResult(interaction.result);
        break;
      case "RunSuggestion":
        applySuggestion(interaction.suggestion);
        break;
      case "ToggleSuggestions":
        applyToggleSuggestions();
        break;
      case "DockSelected":
        applyDockSelection(interaction.section);
        break;
      case "ModeChanged":
        setMode(interaction.mode);
        break;
      case "OpenOutput":
        void applyOpenCurrentOutput();
        break;
      case "CopyOutput":
        void applyCopyOutput();
        break;
      case "ShareOutput":
        applyShareOutput();
        break;
      case "ExpandOutput":
        applyExpandOutput();
        break;
      case "ResetWorkspace":
        applyResetWorkspace();
        break;
      default:
        break;
    }
  }

  function applySelectResult(result: SearchResult) {
    setSelectedResult(result);
    setOutput(previewForResult(result));
    setPhase("analyze");
    setProgress(0.58);
    addLog("info", `Selected result ${resultSource(result)}`);
  }

  function applySuggestion(suggestion: SuggestedAction) {
    setPhase(suggestion.action.includes("brief") ? "synthesize" : "analyze");
    setProgress(suggestion.action.includes("brief") ? 0.78 : 0.62);
    addLog("info", `Action: ${suggestion.title}`);
    const targetResult = selectedResult ?? results[0] ?? null;

    if ((suggestion.action === "open_result" || suggestion.action === "open") && targetResult) {
      setSelectedResult(targetResult);
      setOutput(previewForResult(targetResult));
      void openSource(resultSource(targetResult));
      return;
    }

    if (suggestion.action === "compare" || suggestion.action === "compare_candidates") {
      const sourceSummary = results.slice(0, 3).map(resultSource);
      setOutput({
        title: "Retrieval Comparison",
        summary: "Skygrep is showing ranked, bounded context instead of a broad line dump. Use the score and source type to decide whether to inspect or refine.",
        readiness: quality === "uncertain" ? "Uncertain" : "Ready",
        sources: sourceSummary,
      });
      return;
    }

    if (suggestion.action === "proactive_search") {
      const sourceSummary = results.slice(0, 4).map(resultSource);
      setOutput({
        title: "Proactive Search Candidate",
        summary: "Skygrep found a likely explanatory path before the user asks the follow-up: benchmark gap, parity baseline, and token-savings measurement can be inspected as one evidence chain.",
        readiness: quality === "uncertain" ? "Uncertain" : "Ready",
        sources: sourceSummary,
      });
      return;
    }

    if (suggestion.action === "attach_evidence") {
      const sourceSummary = results.slice(0, 5).map(resultSource);
      setOutput({
        title: "Attached Evidence Pack",
        summary: `SkyGrab auto-attached the strongest local paths for "${submittedQuery}". These sources now ground Open, Share, and brief generation actions.`,
        readiness: sourceSummary.length ? "Ready" : "Needs evidence",
        sources: sourceSummary,
      });
      setPhase("analyze");
      setProgress(0.66);
      return;
    }

    if (suggestion.action === "explain_router") {
      const route = telemetry
        ? `${telemetry.path}; gap ${telemetry.gap.toFixed(4)} vs tau ${telemetry.tau.toFixed(4)}; mode ${telemetry.tauMode}`
        : `terminal auto diversion inferred from ${results[0]?.sourceType ?? "pending evidence"}`;
      setOutput({
        title: "SkyGrab Router Trace",
        summary: `Intent was recognized as ${commandSignal.intent}. The active route is ${route}. The UI is showing path attachment, confidence, and follow-up actions from the same evidence stream.`,
        readiness: quality === "uncertain" ? "Needs review" : "Ready",
        sources: results.slice(0, 4).map(resultSource),
      });
      setPhase("analyze");
      setProgress(0.7);
      return;
    }

    if (suggestion.action === "detail_selected") {
      const path = targetResult ? resultSource(targetResult) : results[0] ? resultSource(results[0]) : "";
      setOutput({
        title: "Detail Request",
        summary: path
          ? `Next focused command: skygrep --content --detail full --include "${targetResult?.path ?? path}" "${submittedQuery}". Use this when the user needs full context after the ranked path is known.`
          : `No path is selected yet. Run the terminal route first, then request detail from the strongest evidence path.`,
        readiness: path ? "Ready to run" : "Needs evidence",
        sources: path ? [path] : [],
      });
      setPhase("analyze");
      setProgress(0.68);
      return;
    }

    if (suggestion.action.startsWith("mcp")) {
      setOutput({
        title: "MCP Workflow Candidate",
        summary: "This is a future tool route, not a local search result: the desktop agent can branch into GitHub, Linear, Slack, docs, or browser connectors when local evidence is not enough.",
        readiness: "Planned",
        sources: results.slice(0, 2).map(resultSource),
      });
      return;
    }

    if (suggestion.action === "workflow_alternative") {
      setOutput({
        title: "Workflow Alternative",
        summary: "Alternative route prepared: inspect parity-vs-ripgrep first, then compare benchmark token budgets, then synthesize the uncertainty notes.",
        readiness: "Ready",
        sources: results.slice(0, 3).map(resultSource),
      });
      return;
    }

    if (suggestion.action === "automation_watch") {
      setOutput({
        title: "Benchmark Drift Watch",
        summary: "Automation candidate staged. In a later release this becomes a background watcher over benchmark files and index freshness signals.",
        readiness: "Planned",
        sources: results.slice(0, 3).map(resultSource),
      });
      return;
    }

    if ((suggestion.action === "summarize_file" || suggestion.action === "brief" || suggestion.action === "create_brief") && targetResult) {
      setOutput({
        title: suggestion.action === "create_brief" || suggestion.action === "brief"
          ? "Grounded Output Brief"
          : `Summary: ${targetResult.path.split("/").pop()}`,
        summary: `The current answer is grounded in ${targetResult.path}. The route is ${telemetry?.path ?? targetResult.sourceType}; top score is ${formatScore(targetResult.score)} with ${telemetry ? `gap ${telemetry.gap.toFixed(4)} vs tau ${telemetry.tau.toFixed(4)}` : "visible ranked evidence"}.`,
        readiness: targetResult.score >= 0.7 ? "Ready" : "Needs review",
        sources: results.slice(0, 4).map(resultSource),
      });
      return;
    }

    setOutput({
      title: "Output Brief",
      summary: `Prepared a short brief for "${submittedQuery}" using ${results.length} visible ranked source(s).`,
      readiness: quality === "uncertain" ? "Needs review" : "Ready",
      sources: results.slice(0, 4).map(resultSource),
    });
  }

  function applyToggleSuggestions() {
    setExpandedSuggestions((current) => !current);
    addLog("info", "Toggled expanded suggestions");
  }

  async function applyOpenCurrentOutput() {
    const source = output.sources[0] ?? selectedResult?.path;
    setPhase("output");
    setProgress(1);
    if (!source) {
      addLog("warn", "Open requested but no source is selected");
      return;
    }
    await openSource(source);
  }

  async function openSource(source: string) {
    if (isTauriRuntime()) {
      try {
        await invoke("open_source", { source });
        addLog("info", `Opened ${source}`);
        return;
      } catch (error) {
        addLog("warn", `Could not open ${source}: ${String(error)}`);
        return;
      }
    }
    addLog("info", `Open requested for ${source}`);
  }

  async function applyCopyOutput() {
    const payload = `${output.title}\n\n${output.summary}\n\nSources:\n${output.sources.join("\n")}`;
    try {
      if (isTauriRuntime()) {
        await invoke("copy_text", { text: payload });
      } else {
        await navigator.clipboard?.writeText(payload);
      }
      addLog("info", "Copied output preview to clipboard");
    } catch {
      addLog("warn", "Clipboard unavailable; output preview remains visible");
    }
  }

  function applyShareOutput() {
    setOutput((current) => ({
      ...current,
      readiness: current.readiness === "Uncertain" ? "Needs review" : "Ready",
      summary: `${current.summary} Share package prepared with visible sources and confidence state.`,
    }));
    addLog("info", "Prepared share package from current preview");
  }

  function applyExpandOutput() {
    setPhase("output");
    setProgress(1);
    setOutput((current) => ({
      ...current,
      summary: `${current.summary} Expanded preview is active; visible sources and router state are preserved across native windows.`,
    }));
    addLog("info", "Expanded output preview context");
  }

  function applyResetWorkspace() {
    setPanels(defaultPanels);
    setActiveDock("Command");
    setExpandedSuggestions(false);
    addLog("info", "Reset workspace layout and dock state");
  }

  async function hideDesktop() {
    addLog("info", "Hiding Skygrep Desktop");
    if (isTauriRuntime()) {
      try {
        await invoke("hide_native_panels");
      } catch (error) {
        addLog("warn", `Could not hide desktop panels: ${String(error)}`);
      }
    }
  }

  function applyDockSelection(section: DockSection) {
    setActiveDock(section);
    setPhase(section === "Command" ? "search" : section === "Workflows" ? "analyze" : "output");
    setProgress(section === "Command" ? 0.24 : section === "Workflows" ? 0.58 : 1);
    setOutput(previewForDock(section, results.slice(0, 3).map(resultSource)));
    addLog("info", `Dock section selected: ${section}`);
  }

  const selectResult = (result: SearchResult) => publishInteraction({ type: "SelectResult", result });
  const handleSuggestion = (suggestion: SuggestedAction) => {
    publishInteraction({ type: "RunSuggestion", suggestion });
    if (isTauriRuntime() && suggestion.action === "detail_selected") {
      const source = selectedResult?.path ?? results[0]?.path;
      if (!source) {
        addLog("warn", "Detail route needs a selected Skygrep evidence path");
        return;
      }
      addLog("info", `Calling Skygrep detail route for ${source}`);
      void invoke<RunSummary>("run_detail_search", { query: submittedQuery || query, source })
        .catch((error) => {
          setQuality("uncertain");
          addLog("error", `Skygrep detail route failed: ${String(error)}`);
        });
      return;
    }
    if (isTauriRuntime() && suggestion.action === "proactive_search" && !running) {
      const source = selectedResult?.path ?? results[0]?.path;
      const followUp = source
        ? `${submittedQuery || query} follow-up evidence in ${source}`
        : `${submittedQuery || query} follow-up evidence`;
      addLog("info", "Running proactive Skygrep follow-up route");
      void invoke<RunSummary>("run_agent", { query: followUp })
        .catch((error) => {
          setQuality("uncertain");
          addLog("error", `Proactive Skygrep route failed: ${String(error)}`);
        });
      return;
    }
    if (isTauriRuntime() && (suggestion.action === "brief" || suggestion.action === "create_brief")) {
      addLog("info", "Calling Skygrep terminal answer route");
      void invoke<OutputPreview>("run_answer", { query: submittedQuery || query })
        .catch((error) => {
          setQuality("uncertain");
          addLog("error", `Skygrep answer route failed: ${String(error)}`);
        });
    }
  };
  const showMoreSuggestions = () => publishInteraction({ type: "ToggleSuggestions" });
  const openCurrentOutput = () => publishInteraction({ type: "OpenOutput" });
  const copyOutput = () => publishInteraction({ type: "CopyOutput" });
  const shareOutput = () => publishInteraction({ type: "ShareOutput" });
  const expandOutput = () => publishInteraction({ type: "ExpandOutput" });
  const selectDock = (section: DockSection) => publishInteraction({ type: "DockSelected", section });
  const setVisualMode = (nextMode: VisualMode) => publishInteraction({ type: "ModeChanged", mode: nextMode });
  const resetWorkspace = () => {
    publishInteraction({ type: "ResetWorkspace" });
    if (isTauriRuntime()) {
      void invoke("restore_native_layout").catch(() => {
        addLog("warn", "Could not restore native window layout");
      });
    }
  };

  if (isTauriRuntime() && surface === "shell") {
    return <DesktopBackdrop mode={mode} />;
  }

  if (surface !== "shell") {
    return (
      <main className={`native-window native-${surface} mode-${mode}`} data-testid={`native-${surface}`}>
        {surface === "topbar" && (
          <TopBar
            mode={mode}
            setMode={setVisualMode}
            quality={quality}
            rootPath={rootPath}
            onResetLayout={resetWorkspace}
            onClose={hideDesktop}
          />
        )}
        {surface === "live-search" && (
          <LiveSearchPanel
            results={results}
            selectedResult={selectedResult}
            query={submittedQuery}
            rootPath={rootPath}
            pinned={panels.liveSearch.pinned}
            onSelectResult={selectResult}
            onPin={() => updatePanel("liveSearch", { pinned: !panels.liveSearch.pinned })}
          />
        )}
        {surface === "logs" && (
          <LogsPanel
            logs={logs}
            pinned={panels.logs.pinned}
            onPin={() => updatePanel("logs", { pinned: !panels.logs.pinned })}
          />
        )}
        {surface === "center" && (
          activeDock === "Workflows" ? (
            <WorkflowPanel graph={graph} />
          ) : (
            <SearchIntelligencePanel
              query={submittedQuery}
              results={results}
              selectedResult={selectedResult}
              quality={quality}
              telemetry={telemetry}
              indexedFileCount={indexedFileCount}
              candidateCount={candidateCount}
              running={running}
              suggestions={suggestions}
              signal={commandSignal}
              attachedCount={attachedSources(results, output).length}
              onSelectResult={selectResult}
            />
          )
        )}
        {surface === "command" && (
          <CommandBox
            query={query}
            setQuery={setQuery}
            submit={submit}
            running={running}
            signal={commandSignal}
            hasResults={results.length > 0}
            onAction={handleSuggestion}
          />
        )}
        {surface === "agent" && (
          <AgentStatus
            phase={phase}
            progress={progress}
            quality={quality}
            running={running}
            onDragStart={startWindowDrag}
          />
        )}
        {surface === "suggestions" && (
          <SuggestionsPanel
            suggestions={suggestions}
            expanded={expandedSuggestions}
            onAction={handleSuggestion}
            onShowMore={showMoreSuggestions}
          />
        )}
        {surface === "output" && (
          <OutputPreviewPanel
            output={output}
            onOpen={openCurrentOutput}
            onOpenSource={openSource}
            onCopy={copyOutput}
            onShare={shareOutput}
            onExpand={expandOutput}
          />
        )}
        {surface === "dock" && <BottomDock active={activeDock} onSelect={selectDock} compact />}
        {surface !== "topbar" && <ResizeHandles />}
      </main>
    );
  }

  return (
    <main
      className={`desktop-shell mode-${mode}`}
      data-testid="desktop-shell"
    >
      <div className="wallpaper" />
      <div className="depth-field" />
      <TopBar
        mode={mode}
        setMode={setVisualMode}
        quality={quality}
        rootPath={rootPath}
        onResetLayout={resetWorkspace}
        onClose={hideDesktop}
      />

      {customizing && (
        <PanelCustomizer
          panels={panels}
          updatePanel={updatePanel}
          movePanel={movePanel}
          reset={() => setPanels(defaultPanels)}
        />
      )}

      <section className="layout-grid" aria-label="Skygrep Desktop workspace">
        <div className="left-stack">
          {visiblePanels.includes("liveSearch") && (
            <LiveSearchPanel
              results={results}
              selectedResult={selectedResult}
              query={submittedQuery}
              rootPath={rootPath}
              pinned={panels.liveSearch.pinned}
              onSelectResult={selectResult}
              onPin={() => updatePanel("liveSearch", { pinned: !panels.liveSearch.pinned })}
            />
          )}
          {visiblePanels.includes("logs") && (
            <LogsPanel
              logs={logs}
              pinned={panels.logs.pinned}
              onPin={() => updatePanel("logs", { pinned: !panels.logs.pinned })}
            />
          )}
        </div>

        <div className="center-stage">
          {visiblePanels.includes("workflow") && (
            activeDock === "Workflows" ? (
              <WorkflowPanel graph={graph} />
            ) : (
              <SearchIntelligencePanel
                query={submittedQuery}
                results={results}
                selectedResult={selectedResult}
                quality={quality}
                telemetry={telemetry}
                indexedFileCount={indexedFileCount}
                candidateCount={candidateCount}
                running={running}
                suggestions={suggestions}
                signal={commandSignal}
                attachedCount={attachedSources(results, output).length}
                onSelectResult={selectResult}
              />
            )
          )}
          <CommandBox
            query={query}
            setQuery={setQuery}
            submit={submit}
            running={running}
            signal={commandSignal}
            hasResults={results.length > 0}
            onAction={handleSuggestion}
          />
          {visiblePanels.includes("agent") && (
            <AgentStatus
              phase={phase}
              progress={progress}
              quality={quality}
              running={running}
              onDragStart={startWindowDrag}
            />
          )}
        </div>

        <div className="right-stack">
          {visiblePanels.includes("suggestions") && (
            <SuggestionsPanel
              suggestions={suggestions}
              expanded={expandedSuggestions}
              onAction={handleSuggestion}
              onShowMore={showMoreSuggestions}
            />
          )}
          {visiblePanels.includes("output") && (
            <OutputPreviewPanel
              output={output}
              onOpen={openCurrentOutput}
              onOpenSource={openSource}
              onCopy={copyOutput}
              onShare={shareOutput}
              onExpand={expandOutput}
            />
          )}
        </div>
      </section>

      <BottomDock active={activeDock} onSelect={selectDock} />
    </main>
  );
}

function TopBar({
  mode,
  setMode,
  quality,
  rootPath,
  onResetLayout,
  onClose,
}: {
  mode: VisualMode;
  setMode: (mode: VisualMode) => void;
  quality: SearchQuality;
  rootPath: string;
  onResetLayout: () => void;
  onClose: () => void;
}) {
  const [clock, setClock] = useState(() => new Date());

  useEffect(() => {
    const id = window.setInterval(() => setClock(new Date()), 30_000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <header className="top-bar">
      <div className="brand" data-tauri-drag-region onPointerDown={startWindowDrag}>
        <Sparkles size={22} />
        <strong>Skygrep Desktop</strong>
        <span className="divider" />
        <span className="shortcut">⌘⇧ Space</span>
        <span className="muted">Global Shortcut</span>
        <span className="root-badge" title={rootPath}>{shortRoot(rootPath)}</span>
      </div>
      <div className="top-actions" onPointerDown={(event) => event.stopPropagation()}>
        <Segmented
          value={mode}
          left="focus"
          right="3d"
          onChange={setMode}
        />
        <button className="icon-button" onClick={onResetLayout} aria-label="Reset layout">
          <RotateCcw size={16} />
        </button>
        <span className={`quality-pill ${quality}`}>{quality}</span>
        <Moon size={17} />
        <span className="muted">{clock.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span>
        <button className="icon-button close-button" onClick={onClose} aria-label="Hide Skygrep Desktop">
          <X size={17} />
        </button>
      </div>
    </header>
  );
}

function DesktopBackdrop({ mode }: { mode: VisualMode }) {
  return (
    <main className={`desktop-backdrop mode-${mode}`} aria-hidden="true">
      <div className="wallpaper" />
      <div className="depth-field" />
      <div className="ambient-path one" />
      <div className="ambient-path two" />
      <div className="ambient-keywords">
        <span>semantic</span>
        <span>parity</span>
        <span>token savings</span>
        <span>workflow</span>
        <span>MCP</span>
      </div>
    </main>
  );
}

function Segmented<T extends string>({
  value,
  left,
  right,
  onChange,
}: {
  value: T;
  left: T;
  right: T;
  onChange: (value: T) => void;
}) {
  return (
    <div className="segmented" role="tablist" aria-label="Visual mode">
      <button className={value === left ? "active" : ""} onClick={() => onChange(left)}>{left === "3d" ? "3D" : "Focus"}</button>
      <button className={value === right ? "active" : ""} onClick={() => onChange(right)}>{right === "3d" ? "3D" : "Focus"}</button>
    </div>
  );
}

function PanelCustomizer({
  panels,
  updatePanel,
  movePanel,
  reset,
}: {
  panels: PanelState;
  updatePanel: (panel: PanelId, patch: Partial<PanelState[PanelId]>) => void;
  movePanel: (panel: PanelId, delta: number) => void;
  reset: () => void;
}) {
  return (
    <aside className="customizer glass-panel">
      <div className="panel-title">
        <span>Modular Layout</span>
        <button onClick={reset}>Reset</button>
      </div>
      {Object.keys(panels).map((key) => {
        const panel = key as PanelId;
        return (
          <div className="custom-row" key={panel}>
            <label>
              <input
                type="checkbox"
                checked={panels[panel].visible}
                onChange={(event) => updatePanel(panel, { visible: event.target.checked })}
              />
              {panelLabels[panel]}
            </label>
            <div>
              <button onClick={() => updatePanel(panel, { pinned: !panels[panel].pinned })}>
                {panels[panel].pinned ? "Pinned" : "Pin"}
              </button>
              <button onClick={() => movePanel(panel, -1)}>↑</button>
              <button onClick={() => movePanel(panel, 1)}>↓</button>
            </div>
          </div>
        );
      })}
    </aside>
  );
}

function Panel({
  title,
  icon,
  children,
  className = "",
  live,
  action,
}: {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  live?: boolean;
  action?: React.ReactNode;
}) {
  return (
    <section
      className={`glass-panel depth-window ${className}`}
      onPointerMove={tiltWindow}
      onPointerLeave={resetTilt}
    >
      <header className="panel-header" onPointerDown={startWindowDrag}>
        <div className="panel-title" data-tauri-drag-region>
          {icon}
          <span>{title}</span>
        </div>
        <div className="panel-actions" onPointerDown={(event) => event.stopPropagation()}>
          {live && <span className="live-dot">Live</span>}
          {action}
        </div>
      </header>
      {children}
    </section>
  );
}

function ResizeHandles() {
  const handles: Array<[ResizeDirection, string]> = [
    ["North", "n"],
    ["East", "e"],
    ["South", "s"],
    ["West", "w"],
    ["NorthEast", "ne"],
    ["NorthWest", "nw"],
    ["SouthEast", "se"],
    ["SouthWest", "sw"],
  ];
  return (
    <div className="resize-frame" aria-hidden="true">
      {handles.map(([direction, className]) => (
        <button
          className={`resize-handle ${className}`}
          key={direction}
          onPointerDown={(event) => startResize(direction, event)}
          tabIndex={-1}
          type="button"
        />
      ))}
    </div>
  );
}

function LiveSearchPanel({
  results,
  selectedResult,
  query,
  rootPath,
  pinned,
  onSelectResult,
  onPin,
}: {
  results: SearchResult[];
  selectedResult: SearchResult | null;
  query: string;
  rootPath: string;
  pinned: boolean;
  onSelectResult: (result: SearchResult) => void;
  onPin: () => void;
}) {
  const [filter, setFilter] = useState("All");
  const filteredResults = useMemo(() => {
    if (filter === "All") return results;
    const normalized = filter.toLowerCase();
    return results.filter((result) => {
      const haystack = `${result.path} ${result.language ?? ""} ${result.sourceType} ${result.snippet}`.toLowerCase();
      if (normalized === "code") return Boolean(result.language) || /\.(rs|py|ts|tsx|js|jsx|go|swift|kt|java|c|cpp|h|hpp)$/i.test(result.path);
      if (normalized === "docs") return /\.(md|mdx|txt|rst|docx|pdf)$/i.test(result.path) || haystack.includes("doc");
      if (normalized === "benchmarks") return haystack.includes("benchmark") || result.path.includes("benchmarks/");
      if (normalized === "internal") return !/^https?:\/\//.test(result.path);
      return true;
    });
  }, [filter, results]);

  return (
    <Panel
      title="Live Search"
      icon={<Zap size={20} />}
      live
      className="live-search"
      action={<PinButton pinned={pinned} onClick={onPin} />}
    >
      <div className="search-field">
        <Search size={15} />
        <span>{query}</span>
      </div>
      <div className="root-strip">
        <span>Root</span>
        <code title={rootPath}>{rootPath}</code>
      </div>
      <div className="filter-row">
        {["All", "Code", "Docs", "Benchmarks", "Internal"].map((label) => (
          <button
            className={filter === label ? "chip active" : "chip"}
            key={label}
            onClick={() => setFilter(label)}
            type="button"
          >
            {label}
          </button>
        ))}
      </div>
      <div className="result-list">
        {filteredResults.length === 0 ? (
          <div className="empty-state">Waiting for ranked local evidence...</div>
        ) : (
          filteredResults.slice(0, 8).map((result) => (
            <ResultCard
              result={result}
              selected={selectedResult?.path === result.path && selectedResult?.startLine === result.startLine}
              onSelect={() => onSelectResult(result)}
              key={`${result.path}-${result.startLine}`}
            />
          ))
        )}
      </div>
    </Panel>
  );
}

function PinButton({ pinned, onClick }: { pinned: boolean; onClick: () => void }) {
  return (
    <button className={`icon-button small ${pinned ? "active" : ""}`} onClick={onClick} aria-label="Pin panel">
      <Pin size={14} />
    </button>
  );
}

function ResultCard({
  result,
  selected,
  onSelect,
}: {
  result: SearchResult;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button className={`result-card ${selected ? "selected" : ""}`} onClick={onSelect}>
      <div className="result-topline">
        <Braces size={15} />
        <strong>{result.path}</strong>
        <span>{formatScore(result.score)}</span>
      </div>
      <div className="result-meta">
        <span>{result.language ?? "text"}</span>
        <span>{result.startLine ? `${result.startLine}-${result.endLine ?? result.startLine}` : "file"}</span>
        <span>{result.sourceType}</span>
      </div>
      <p>{result.snippet}</p>
    </button>
  );
}

function LogsPanel({
  logs,
  pinned,
  onPin,
}: {
  logs: Array<{ level: LogLevel; message: string; at: string }>;
  pinned: boolean;
  onPin: () => void;
}) {
  return (
    <Panel
      title="Streaming Logs"
      icon={<Activity size={20} />}
      live
      className="logs-panel"
      action={<PinButton pinned={pinned} onClick={onPin} />}
    >
      <div className="log-lines">
        {logs.map((log, index) => (
          <div className={`log-line ${log.level}`} key={`${log.at}-${index}`}>
            <span>{log.at}</span>
            <code>{log.message}</code>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function SearchIntelligencePanel({
  query,
  results,
  selectedResult,
  quality,
  telemetry,
  indexedFileCount,
  candidateCount,
  running,
  suggestions,
  signal,
  attachedCount,
  onSelectResult,
}: {
  query: string;
  results: SearchResult[];
  selectedResult: SearchResult | null;
  quality: SearchQuality;
  telemetry: CascadeTelemetry | null;
  indexedFileCount: number | null;
  candidateCount: number | null;
  running: boolean;
  suggestions: SuggestedAction[];
  signal: CommandSignal;
  attachedCount: number;
  onSelectResult: (result: SearchResult) => void;
}) {
  const topScore = results[0]?.score ?? 0;
  const routerLabel = telemetry
    ? `${telemetry.path}${telemetry.earlyExit ? " · early exit" : " · escalated"}`
    : running
      ? "routing search..."
      : "semantic cascade ready";
  const explainedResults = results.length
    ? results.slice(0, 3)
    : selectedResult
      ? [selectedResult]
      : [];
  const proactiveSuggestion = suggestions.find((suggestion) => (
    suggestion.action.includes("proactive") || suggestionKind(suggestion) === "Proactive"
  )) ?? suggestions[0];
  return (
    <Panel title="Search Intelligence" icon={<Radar size={19} />} live className="search-intelligence-panel">
      <div className="intel-hero">
        <div>
          <span className={`quality-pill ${quality}`}>{quality}</span>
          <h2>{results.length ? `${results.length} ranked evidence paths` : "Waiting for ranked evidence"}</h2>
          <p>{query}</p>
        </div>
        <div className="confidence-ring" style={{ "--confidence": `${Math.max(14, topScore * 100)}%` } as React.CSSProperties}>
          <strong>{formatScore(topScore || 0.74)}</strong>
          <span>confidence</span>
        </div>
      </div>

      <div className="evidence-map" aria-label="Search result evidence map">
        {results.slice(0, 4).map((result, index) => (
          <button
            className={`evidence-node ${selectedResult?.path === result.path ? "selected" : ""}`}
            style={{ "--node-index": index } as React.CSSProperties}
            key={`${result.path}-${result.startLine}`}
            onClick={() => onSelectResult(result)}
          >
            <CircleDot size={16} />
            <strong>{result.path.split("/").pop()}</strong>
            <span>{formatScore(result.score)} · {result.sourceType}</span>
          </button>
        ))}
        <div className="evidence-beam one" />
        <div className="evidence-beam two" />
        <div className="evidence-beam three" />
      </div>

      <div className="router-strip">
        <span>
          <strong>Router</strong>
          {routerLabel}
        </span>
        <span>
          <strong>Gap</strong>
          {telemetry ? `${telemetry.gap.toFixed(4)} / τ ${telemetry.tau.toFixed(4)}` : "pending"}
        </span>
        <span>
          <strong>Pool</strong>
          {indexedFileCount ?? "?"} files · {candidateCount ?? results.length} candidates
        </span>
      </div>

      <div className="capability-grid" aria-label="SkyGrab capability state">
        <div>
          <span>Intent</span>
          <strong>{signal.intent}</strong>
          <em>{Math.round(signal.confidence * 100)}% recognized</em>
        </div>
        <div>
          <span>Diversion</span>
          <strong>{signal.route}</strong>
          <em>terminal behavior mirrored</em>
        </div>
        <div>
          <span>Attachment</span>
          <strong>{attachedCount} path{attachedCount === 1 ? "" : "s"}</strong>
          <em>auto-bound to preview</em>
        </div>
      </div>

      {proactiveSuggestion && (
        <div className="proactive-intel">
          <span>Proactive Search</span>
          <strong>{proactiveSuggestion.title}</strong>
          <p>{proactiveSuggestion.description}</p>
        </div>
      )}

      <div className="evidence-explainers">
        {explainedResults.map((result, index) => (
          <button key={`${result.path}-${index}-why`} onClick={() => onSelectResult(result)}>
            <strong>{index + 1}. {result.path.split("/").pop()}</strong>
            <span>
              {explainResult(result, telemetry)}
            </span>
          </button>
        ))}
      </div>

      <div className="workflow-runway" aria-label="SkyGrab workflow">
        {[
          ["Recognize", signal.intent],
          ["Route", signal.route],
          ["Retrieve", `${results.length} paths`],
          ["Attach", `${attachedCount} sources`],
          ["Act", signal.nextStep],
        ].map(([label, detail], index) => (
          <div className={index <= Math.max(0, phaseOrder.indexOf(running ? "search" : "output")) ? "active" : ""} key={label}>
            <strong>{label}</strong>
            <span>{detail}</span>
          </div>
        ))}
      </div>

      <footer className="intel-footer">
        <span>{telemetry ? `${telemetry.tauMode} router telemetry` : "Default surface explains search evidence"}</span>
        <span>{activeWorkflowLabel(quality)}</span>
      </footer>
    </Panel>
  );
}

function explainResult(result: SearchResult, telemetry: CascadeTelemetry | null) {
  const parts = [
    `${formatScore(result.score)} ${result.sourceType}`,
    result.semanticScore != null ? `semantic ${result.semanticScore.toFixed(3)}` : null,
    result.lexicalScore != null ? `lexical ${result.lexicalScore.toFixed(3)}` : null,
    result.symbolBoost != null ? `symbol +${result.symbolBoost.toFixed(2)}` : null,
    telemetry ? `route ${telemetry.path}` : null,
  ].filter(Boolean);
  return parts.join(" · ");
}

function activeWorkflowLabel(quality: SearchQuality) {
  if (quality === "uncertain") return "follow-up needed";
  if (quality === "degraded") return "degraded retrieval";
  return "ready for synthesis";
}

function WorkflowPanel({ graph }: { graph: WorkflowGraph }) {
  const nodes = graph.nodes;
  return (
    <Panel title="Workflow Graph" icon={<GitBranch size={19} />} live className="workflow-panel">
      <div className="graph-canvas">
        {nodes.map((node, index) => (
          <div className={`graph-node ${node.state}`} key={node.id} style={{ "--node-index": index } as React.CSSProperties}>
            <BadgeCheck size={16} />
            <strong>{node.label}</strong>
            <span>{node.detail}</span>
          </div>
        ))}
        {nodes.slice(0, -1).map((node, index) => (
          <div className="graph-edge" style={{ "--edge-index": index } as React.CSSProperties} key={`${node.id}-edge`} />
        ))}
      </div>
      <footer className="graph-footer">
        <span>DAG ID: {graph.id}</span>
        <span>{graph.nodes.length} steps</span>
      </footer>
    </Panel>
  );
}

function CommandBox({
  query,
  setQuery,
  submit,
  running,
  signal,
  hasResults,
  onAction,
}: {
  query: string;
  setQuery: (value: string) => void;
  submit: (event?: FormEvent) => void;
  running: boolean;
  signal: CommandSignal;
  hasResults: boolean;
  onAction: (suggestion: SuggestedAction) => void;
}) {
  const actions = [
    quickAction("attach_evidence", "Attach paths", "Attach the strongest SkyGrab evidence paths to the output preview.", 0.9),
    quickAction("explain_router", "Explain route", "Show intent, diversion lane, gap, tau, and provenance.", signal.confidence),
    quickAction("detail_selected", "Need detail", signal.detailPrompt, hasResults ? 0.82 : 0.42),
  ];
  return (
    <form className="command-box" onSubmit={submit}>
      <div className="native-drag-strip" data-tauri-drag-region onPointerDown={startWindowDrag} />
      <div className="command-logo">
        {running ? <Loader2 className="spin" size={28} /> : <Sparkles size={30} />}
      </div>
      <textarea
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            submit();
          }
        }}
        aria-label="Skygrep command"
        placeholder="Ask Skygrep Desktop to search, analyze, and synthesize..."
      />
      <button className="command-submit" type="submit" aria-label="Run command" disabled={running}>
        <ArrowRight size={24} />
      </button>
      <div className="command-intel">
        <span><strong>Intent</strong>{signal.intent}</span>
        <span><strong>Route</strong>{signal.route}</span>
        <span><strong>Context</strong>{signal.attachment}</span>
        <span><strong>Next</strong>{signal.nextStep}</span>
      </div>
      <div className="command-quick-actions">
        {actions.map((action) => (
          <button
            type="button"
            key={action.action}
            onClick={() => onAction(action)}
          >
            {action.title}
          </button>
        ))}
      </div>
      <div className="command-hints">
        <span>↩ to execute</span>
        <span>esc to cancel</span>
      </div>
    </form>
  );
}

function SuggestionsPanel({
  suggestions,
  expanded,
  onAction,
  onShowMore,
}: {
  suggestions: SuggestedAction[];
  expanded: boolean;
  onAction: (suggestion: SuggestedAction) => void;
  onShowMore: () => void;
}) {
  const visible = expanded ? suggestions : suggestions.slice(0, 3);
  return (
    <Panel title="Proactive Actions" icon={<Sparkles size={19} />} className="suggestions-panel">
      <div className="suggestion-list">
        {visible.map((suggestion, index) => (
          <button className="suggestion-card" key={suggestion.id} onClick={() => onAction(suggestion)}>
            <div className={`suggestion-icon tone-${index % 3}`}>
              {index % 3 === 0 ? <Activity size={20} /> : index % 3 === 1 ? <Boxes size={20} /> : <Bell size={20} />}
            </div>
            <div>
              <em>{suggestionKind(suggestion)}</em>
              <strong>{suggestion.title}</strong>
              <p>{suggestion.description}</p>
              <span>{formatScore(suggestion.confidence)}</span>
            </div>
            <span className="suggestion-arrow" aria-hidden="true">
              <ArrowRight size={17} />
            </span>
          </button>
        ))}
      </div>
      <button className="more-button" onClick={onShowMore}>
        {expanded ? "Show fewer suggestions" : "Show more suggestions"} <ChevronDown size={15} />
      </button>
    </Panel>
  );
}

function OutputPreviewPanel({
  output,
  onOpen,
  onOpenSource,
  onCopy,
  onShare,
  onExpand,
}: {
  output: OutputPreview;
  onOpen: () => void;
  onOpenSource: (source: string) => void;
  onCopy: () => void;
  onShare: () => void;
  onExpand: () => void;
}) {
  return (
    <Panel
      title="Output Preview"
      icon={<Eye size={19} />}
      className="output-panel"
      action={(
        <button className="icon-button small" onClick={onExpand} aria-label="Expand output preview">
          <Maximize2 size={16} />
        </button>
      )}
    >
      <div className="preview-body">
        <div>
          <h2>{output.title}</h2>
          <span className={`ready-pill ${output.readiness.toLowerCase().replace(/\s+/g, "-")}`}>{output.readiness}</span>
          <p>{output.summary}</p>
        </div>
        <div className="preview-art">
          <div className="report-cover">
            <span>Executive Summary</span>
            <strong>Skygrep</strong>
          </div>
        </div>
      </div>
      <div className="source-list">
        {output.sources.map((source) => (
          <button key={source} onClick={() => onOpenSource(source)} type="button">{source}</button>
        ))}
      </div>
      <footer className="preview-actions">
        <button onClick={onOpen}>Open</button>
        <button onClick={onCopy}>Copy Link</button>
        <button className="primary" onClick={onShare}><Share2 size={15} /> Share</button>
      </footer>
    </Panel>
  );
}

function BottomDock({
  active,
  onSelect,
  compact = false,
}: {
  active: DockSection;
  onSelect: (section: DockSection) => void;
  compact?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const icons = [Command, GitBranch, BookOpen, Network, History, Settings] as const;
  const activeIndex = dockLabels.indexOf(active);
  const ActiveIcon = icons[Math.max(0, activeIndex)];

  if (compact) {
    return (
      <nav
        className={`bottom-dock dock-rail ${expanded ? "expanded" : "collapsed"}`}
        aria-label="Skygrep sections"
      >
        <div className="native-drag-strip" data-tauri-drag-region onPointerDown={startWindowDrag} />
        <button
          className="dock-current active"
          onClick={() => setExpanded((current) => !current)}
          aria-label={expanded ? "Collapse navigation" : "Expand navigation"}
          aria-expanded={expanded}
        >
          <ActiveIcon size={20} />
          <span>{active}</span>
          {expanded ? <ChevronLeft size={16} /> : <ChevronRight size={16} />}
        </button>
        {expanded && (
          <div className="dock-menu">
            {dockLabels.map((label, index) => {
              const Icon = icons[index];
              return (
                <button
                  className={active === label ? "active" : ""}
                  key={label}
                  onClick={() => {
                    onSelect(label);
                    setExpanded(false);
                  }}
                >
                  <Icon size={19} />
                  <span>{label}</span>
                </button>
              );
            })}
          </div>
        )}
      </nav>
    );
  }

  return (
    <nav className="bottom-dock" aria-label="Skygrep sections">
      <div className="native-drag-strip" data-tauri-drag-region onPointerDown={startWindowDrag} />
      {dockLabels.map((label, index) => {
        const Icon = icons[index];
        return (
        <button className={active === label ? "active" : ""} key={label} onClick={() => onSelect(label)}>
          <Icon size={22} />
          <span>{label}</span>
        </button>
        );
      })}
      <Layers3 size={20} className="dock-depth" />
      <Terminal size={20} className="dock-terminal" />
    </nav>
  );
}
