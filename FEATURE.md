# Claude Code Capability Migration

Date: 2026-04-28

Objective: migrate Claude Code source build capabilities into Skylake Code as
native Rust behavior, using `./claude-code-source-build/` as the reference and
`./skylake-code/` as the implementation target.

Nexus North Star:

Skylake Code is not a Claude Code clone. It is an ANM-driven AI harness
foundation. Claude Code compatibility is only one input into the design, not the
final product boundary. The long-term system should selectively absorb:

- Claude Code's agent loop: tools, permissions, sessions, slash commands,
  context, memory, model switching, and terminal-native coding workflows.
- Warp's agentic development environment principles: rich terminal UX, clean
  separation between shell and agent conversations, visible multi-agent/task
  management, model/status surfaces, diff/file panels, and interactive
  permission/setup flows.
- Codex's orchestration primitives: controlled sub-agent spawning, parallel
  delegation, role-based workers, task ownership, verification loops, and
  harness-level coordination.

ANM is the top-level management framework for this system. It should decide
when to stay single-agent, when to spawn agents, how to decompose work, which
agent owns which scope, what permissions and memory each agent receives, how
results are verified, when synthesis happens, and when the system should pause,
ask, retry, or escalate. No feature should be added only to mimic another tool;
every feature should strengthen controllable, verifiable, scalable AI harness
management under ANM. AMM naming is reserved only for the lower worker
execution/orchestration manager where legacy code still exposes that term.

Principles:

- Preserve behavior, data contracts, and user workflows; do not copy
  TypeScript/Ink implementation directly.
- Keep implementation native to Skylake's Rust runtime, CLI, and TUI layers.
- Migrate in dependency order: state/contracts first, then runtime hooks, then
  TUI surfaces, then remote/cloud integrations.
- Every transferred capability needs tests, docs, and a non-interactive fallback
  where relevant.

Completed in the current implementation slice:

- Coordination state and owner-checked local locks.
- Task registry, output sidecars, shell task spawn, task status/output/stop
  commands, and agent/remote task records.
- Team file management, member management, allowed paths, and lock-protected
  teammate mailbox commands.
- Remote heartbeat lease acquire/status/heartbeat/release commands.
- Coordinator worker lifecycle and synthesis commands.
- Remote bridge session connect/send/inbox/deliver/interrupt/reconnect plus
  remote permission request/response commands.
- Hook registry execution and audit history, wired into PreToolUse,
  PostToolUse, and PostToolUseFailure.
- Plugin manifest loading for hooks, memory, integrations, and MCP servers.
- Plugin marketplace search/install/policy wiring via RegistryClient,
  PolicyEngine, TrustStore, and signed manifest import.
- Memory scan/extract/sync with secret guard.
- IDE/browser/computer-use/worktree integration adapters.
- Persistent permission RuleStore and DenialBuffer are wired into
  ConversationRuntime authorization, with specialized permission dialog
  rendering from the modal factory.
- Managed settings merge into ConfigLoader, and first-run onboarding is wired
  into interactive REPL startup.
- Remote bridge connect/send/interrupt/reconnect now uses provider-neutral
  WebSocket/HTTP RemoteTransport when a non-local URL is configured.
- `/mcp` now renders the MCP dashboard summary, bootstrapped tool prefixes, and
  saved OAuth credential state for configured servers.
- `/tui` capability report updates for task, team, remote, and extension
  migration state.
- TUI input fixes for slash command Tab completion and transient permission
  prompts that do not stack in the transcript.
- Advanced input parity: Ctrl-R history search, Ctrl-O quick-open, file path
  Tab completion, arrow history cycling outside the slash palette, and optional
  vim-mode editing are implemented in Rust.
- Live panes: `/panes [tasks|teams|remote|diff|transcript]` renders pane
  snapshots and `/panes watch ...` opens a crossterm alternate-screen live
  refresh loop.
- Voice mode: `/voice` now manages local recorder/transcriber commands and
  supports record, transcribe, and dictate workflows without a private cloud
  dependency.
- Local mobile relay: `/mobile token/status/serve` provides token-protected
  same-LAN phone access over Skylake's existing remote bridge records, avoiding
  any dependency on Anthropic's private hosted Claude Code Cloud protocol.
- Skill/command activation: `$skill` mentions auto-load matching `SKILL.md`
  instructions, and unknown slash commands resolve project/user command files
  or skills from the discovered extension paths.
- Subagent hardening: `Agent` and persisted task records carry allowed paths,
  owned paths, worktree path, and sandbox mode so AMM can enforce future worker
  isolation using stable Rust contracts.
- AMM execution: `/amm run` plans role workers, launches parallel local worker
  processes, persists task/coordinator records, streams visible reasoning
  summaries and worker output events into JSONL logs, and exposes `/amm events`,
  `/amm console`, plus `/panes watch amm`.
- Mobile relay hardening: `/mobile token/status/pair/serve` now includes pairing
  codes and a phone-readable AMM event endpoint, so a same-LAN phone can inspect
  remote bridge messages and current worker/reasoning summary streams without
  using Anthropic's private hosted cloud protocol.
- TCMode: Skylake now has a native Rust `SpecialMode::TCMode` surface with
  persisted `TcModeSettings`, `ThinkingLevel`, `/tcmode` as the direct user
  entrypoint, `/tc status/on/off/cycle/mode/level/auto/prep/anm/run` for
  lower-level management, Shift+Tab mode cycling through `Normal > Plan > Think
  > TC`, and adaptive auto-routing into AMM worker orchestration only for
  AMM/ANM-level prompts when TCMode is enabled. TCMode now applies a Rust-native ANM
  framework orchestration layer before AMM execution: it fixes the problem family and
  boundary, classifies source events as admitted/repaired/rejected, defines
  field/evolution stages and observables, and turns the framework state into AMM
  worker topology, dependencies, ownership, and readout contracts. `/tc prep
  <task>` renders this ANM plan without running workers; `/tc run <task>` uses
  it to drive AMM. `/tc anm <path>` records the ANM source location for
  optional substrate evidence runs, and `/tc anm run` executes the configured
  ANM substrate runner adapter only when explicitly requested, persists the substrate run record,
  parses finite-field artifact summaries, and exposes admitted/repaired/rejected
  event counts plus `P_f/Q_f/r_f` verifier values. The REPL footer keeps the
  active model visible, renders mode-specific input-panel state, then switches
  TCMode into an operator channel with AMM state and `/panes watch tc` exposing
  latest AMM events, visible reasoning summaries, ANM framework state,
  optional ANM substrate state, and mobile relay state. The scientific substrate
  implementation remains owned by the ANM repo; Skylake owns the Rust ANM
  orchestration layer, adapter contract, state, and UI wiring.
- Adaptive harness routing: Skylake should not use a brittle raw-vs-agent
  binary. Routing is now modeled as graduated Raw, Light, Module, Tool,
  Agentic, ANM, and TCMode-gated ANM-substrate routes. The policy preserves direct model quality for
  simple knowledge prompts, uses light identity/session context when useful,
  dispatches direct module requests into local surfaces, exposes tools only when
  evidence is likely to improve the answer, escalates edits and debugging to the
  agentic loop, and reserves ANM for explicit multi-agent or TCMode
  orchestration. The ANM substrate route only becomes eligible inside TCMode,
  where the internal coordination layer may deliberately select it.
- Mode UX contract: Normal, Plan, Think, and TC are input-panel states, not
  transcript events. Shift+Tab updates the live panel below the input line and
  must not append `/tc cycle` reports into conversation history. Normal, Plan,
  and Think show only their own lightweight mode panels. TCMode is the only
  mode allowed to reveal TC Secret/ANM-gated status, AMM worker state, or ANM
  bridge controls. Simple prompts inside TCMode still pass through adaptive
  routing first; only AMM/ANM-level routes should trigger TCMode worker
  orchestration automatically. `/route` is a dry-run/explanation surface for
  the adaptive routing decision and should not be required to enter a mode.
- Terminal graph rendering: Skylake now has a native Rust graph renderer for
  Mermaid-style flowcharts, simple edge lists, ANM framework plans, and AMM
  worker topology. `/graph` renders ASCII code graphs, Unicode terminal graphs,
  SVG source, and Kitty/iTerm2 rich terminal image payloads; `/panes graph`
  shows the latest AMM topology or a default Skylake Agent Brain graph.
  `/panes watch diff` also keeps interactive file selection and scroll state
  for a more complete diff browser. Natural-language Mermaid/graph/diagram
  requests now route to a dedicated Graph renderer path: the model is asked for
  strict renderer-compatible graph source, then Skylake immediately displays it
  through the local `/graph` renderer instead of only printing a code block.
- Routing naming correction: raw/direct turns report `Direct model` as the
  coordinator. When the harness policy layer is involved, route traces refer to
  the `ANM routing layer`; AMM remains the worker execution/orchestration
  manager, not the label for the raw/direct route classifier.
- Natural-language module routing: graph is no longer a one-off branch.
  Skylake has an ANM module router that maps requests to local handlers for
  graph, status, model, panes, MCP, voice, route, config, memory, diff, version,
  TCMode, and ANM. Future modules should register here instead of adding
  bespoke routing logic.
- ANM module-router governance: `./skylake-code/docs/anm-module-router-rules/`
  now records the durable naming, route-order, safety, registration, testing,
  and notes contract for all future local capability modules. Raw/direct routes
  must say `Direct model`; any actual coordination policy layer must use ANM
  naming; AMM remains the worker execution/orchestration manager rather than a
  generic route label.
- Dynamic routing direction: the ANM routing framework should evolve from a
  static branch list into a modular capability graph. Future modules should
  declare intent hints, preconditions, effects, evidence needs, risk, UI
  surfaces, and fallback behavior so the router can gradually self-rewire and
  adapt to more route types without losing safety. TCMode is where ANM may
  qualitatively improve routing boundaries for complex multi-agent work by
  defining the problem family, evidence threshold, worker topology, verification
  readout, and escalation/de-escalation conditions before orchestration begins.
- Merged `integration-wave-2` and `wiring-batch-1-permissions` into
  `skylake-code/main`; the seven previously deferred wiring points are now on
  main.
- Removed stale "placeholder/deferred" documentation for the migrated HTTP
  transport, permission dialogs, MCP dashboard, onboarding, managed settings,
  plugin marketplace, and remote transport wiring.
- Verified the merged main branch with `make parity` after the wiring merge.
- Cleaned the Skylake repository structure after the routing/module migration:
  the active CLI crate is now `rust/crates/skylake-cli`, the old Python porting
  workspace is isolated under `legacy/python_porting`, stale generated
  `src/`-era guidance has been updated, Rust CI now runs `make parity`, and
  `.sc/` plus `.DS_Store` are ignored in the shared `.gitignore`.
- Extracted adaptive route selection into a dedicated Rust routing module,
  corrected direct route display to `Direct model`, kept local capability
  calls behind the ANM module router, and added weather/default-location
  routing support as another module example.
- Generalized the weather routing lesson into a `Live Lookup` capability
  module. Weather remains one specialized branch, but any request that depends
  on current external state, recent facts, prices, events, scores, releases,
  availability, or online verification should route through live evidence and
  grounded synthesis instead of being swallowed by raw/direct mode. Stable
  knowledge prompts still preserve base-model quality by staying Direct, and
  local workspace/status/model requests remain protected from web lookup.
- Fixed wide-terminal separator rendering for REPL input/footer rules and
  `/panes` snapshots so status and table divider lines can span the current
  terminal width instead of clamping to the old narrow report width.

Remaining intentional differences:

1. Full hosted-cloud protocol parity for proprietary Claude Code relay
   semantics; current transport is provider-neutral WebSocket/HTTP for
   self-hosted relays plus a local mobile relay for phone access.
2. Rich per-worker PTY controls, deeper AMM policy selection, and remote worker
   scheduling remain future work on top of the now-implemented local parallel
   worker process execution and event console. Skylake shows visible reasoning
   summaries and tool/output events, not private hidden chain-of-thought.
3. A Rust-native ANM finite-field substrate remains future work. TCMode can
   execute and summarize the configured ANM substrate runner adapter as optional
   evidence, but the default TCMode orchestration path is the Rust-native ANM
   framework layer over AMM workers, not an external app runner.

Design exploration added on 2026-04-29:

- Added a standalone Skylake Code OS prototype app in
  `./skylake-code/Prototype/` showing a fused terminal-brain and transparent
  GUI-shell surface. The prototype preserves one shared session context across
  surface focus changes so transcript, task, diff, memory, and AMM state remain
  visually attached instead of forked.
- Updated the prototype direction toward a desktop-first agent surface: the
  default view now keeps a simple bottom terminal/chat input dock and pops up a
  connected multi-agent canvas that simulates AMM-driven automatic worker swarm
  spawning, dependency links, per-step status, and final synthesis. The earlier
  web-style terminal/GUI shell is retained as a separate Web Shell view.
- Added an Electron desktop shell prototype around the React renderer. The shell
  registers `Option+Space`/`Alt+Space` for a floating input launcher and uses
  separate native BrowserWindows for each simulated spawned agent, plus a
  transparent overlay surface for the connected swarm backdrop.
- Refined the desktop prototype away from an OpenSwarm-style in-app canvas and
  toward a Skylake Agent OS model: the Electron main process now owns a
  simulated session graph, each spawned native window is bound to an agent ID
  and session ID, and agent lifecycle updates stream over IPC while the
  transparent HUD visualizes relationships without containing the windows.
- Updated the prototype planner from a fixed six-agent swarm to a prompt-shaped
  dynamic workflow router. Debug, research, UI/design, refactor, docs, release,
  and verification prompts now spawn different native agent window counts and a
  generated dependency graph; this remains a local mock of the intended AMM
  planning contract until wired to the Rust harness runtime.
- Added a real LLM planner path for the desktop prototype using local Ollama by
  default, with optional OpenAI Responses API override. The Ollama planner was
  verified against `qwen3-coder:latest` and returns structured workflow JSON
  that chooses the native agent window topology; when the API call fails, the
  shell falls back to the local router and labels the HUD with
  `fallback-router`.
- Added a lower-left native control panel that lists the dynamically spawned
  agent windows and exposes `Close Run` and `Quit` controls so the transparent
  HUD/background cannot be stranded without a visible close surface.
- Changed the desktop execution model from blocking plan-then-spawn to
  progressive orchestration: submitting a prompt immediately opens a Harness
  Planner window and control HUD, Ollama designs the minimal workflow in the
  background, dependency-ready agent windows emerge progressively, fan-out
  branches run concurrently, and each agent now generates a real local Ollama
  output that feeds the final synthesis window.
- Added `./skylake-code/Prototype/DEEP_COMPREHENSIVE_PLAN.md`, a full product
  and implementation plan covering the current prototype inventory, missing
  runtime capabilities, future Agent OS product shape, UX surfaces, runtime
  event model, session data model, AMM planning rules, security model,
  persistence/resume, Rust runtime integration path, Electron/Tauri/Qt decision,
  roadmap, testing strategy, risks, and definition of done.
- Expanded the Agent OS direction into a proactive ANM operating-layer vision:
  Skylake Code remains the terminal/core engine, ANM becomes the higher-level
  harness brain that understands problem boundaries and chooses the minimum
  useful multi-agent topology, and the desktop shell becomes a native
  visualization/control layer over the same runtime session. Future behavior
  should be proactive in awareness and suggestion, but explicit in risky action.
- Documented that the desktop itself is the agent canvas: `Option+Space` opens a
  small bottom launcher, a Harness Planner window appears immediately, ANM plans
  the workflow, transparent future-styled native agent windows emerge on the
  desktop, directional wiring shows dependencies/fan-out/fan-in, and a
  lower-left hierarchy/control panel keeps the run readable, closeable,
  traceable, and restorable.
- Added hierarchy and readability requirements: visible desktop windows should
  show only the most important active agents, while secondary agents, subagents,
  completed outputs, tool calls, artifacts, and approvals are nested/collapsed
  into a durable hierarchy/archive. Every final result must remain traceable
  back through planner decisions, agent/subagent outputs, tool calls,
  approvals, artifacts, and workflow edges.
- Added future protocol concepts for hierarchy nodes, edge records, visibility
  policy, archive/restore semantics, proactive context suggestions, terminal/GUI
  synchronization, and milestone work needed to connect the Electron prototype
  to the Rust AMM runtime without letting the GUI own the source of truth.
- Advanced the first functional Agent OS MVP inside `./skylake-code/Prototype/`:
  Electron now persists the latest session graph to `userData`, agent windows
  can be focused, collapsed, and restored from the lower-left hierarchy panel,
  closing an agent window collapses it instead of deleting the session record,
  and each agent window exposes Output, Logs, and Handoff tabs for traceability.
  `npm run build` passes for this prototype slice.
- Upgraded the desktop launcher toward a compact ChatGPT-style high-resolution
  popup: smaller two-row command bar, add/search/attach/tools controls, model
  selector, voice controls, quit, and send button. The selected Ollama model and
  launch flags are now part of the request and are used by the Electron-owned
  prototype planner and per-agent generation path.
- Hardened the desktop prototype launch behavior and visual system: Electron now
  uses a single-instance lock so `Option+Space` cannot show multiple launcher
  sizes from stale app instances, a separate top-right HUD control can close the
  Agent OS background directly, agent window placement is deterministic instead
  of scattered, and the overlay/window aesthetic moved toward a warmer
  translucent OpenAI OS-style glass surface.
- Reworked the launcher interaction model into two small transparent native
  windows instead of one oversized launcher surface: the visible bottom window
  only contains the medium-small GPT-style black capsule, while
  Model/Search/Tools/Context/Canvas/Voice menus open in a separate upward
  popover window. This removes the visible gray rectangular backing that came
  from reserving transparent menu space inside the launcher bounds. Canvas can
  now be enabled per run or shown/hidden live from the launcher menu, and all
  launcher selections are included in the session launch payload.
- Removed the launcher window's macOS vibrancy material and native window shadow
  from the capsule path. The current Electron implementation follows the
  transparent-window pattern directly: `frame: false`, `transparent: true`,
  `backgroundColor: "#00000000"`, no native shadow, and no large empty window
  bounds behind the capsule. Only the sharp black assistant capsule and active
  popover should be visible.
- Published and advanced `local-mgrep` as a local-first semantic search package:
  0.1.0 reached production PyPI, while the local 0.2.0 release artifacts add
  `.mgrepignore`, stale index cleanup, line-provenance JSON, original-style
  local flags (`-m`, content toggles, language/include/exclude filters), local
  Ollama answer synthesis, bounded local agentic query decomposition, vectorized
  SQLite/NumPy retrieval, and batch embedding. The capability guide now lives at
  `docs/local-mgrep-0.2.0.md`; cloud/login/web-search parity remains explicitly
  out of scope for the free local version.
